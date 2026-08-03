"""
sorter.py — Face recognition logic for KinderSort.

PhotoSorter loads reference encodings and sorts event photos into per-student
output folders.  All processing is CPU-only (no GPU required).
"""

import logging
from collections.abc import Callable
from pathlib import Path

import face_recognition
import numpy as np
from PIL import Image, UnidentifiedImageError

from utils import (
    build_output_filename,
    collect_event_images,
    is_image_file,
    safe_copy,
)


class PhotoSorter:
    """Encapsulates the full sort pipeline from reference loading to file copying.

    Usage::

        sorter = PhotoSorter(reference_folder, events_folder, output_folder, logger)
        skipped_names = sorter.load_references()   # sync, may show warnings
        summary = sorter.sort_all(progress_cb, cancelled_cb)
    """

    DISTANCE_THRESHOLD = 0.455
    """Maximum face distance to consider a match (lower = stricter)."""

   MAX_IMAGE_DIMENSION = 1200
    """Longest side in pixels after resizing for face detection (performance)."""

   def _init_(
        self,
        reference_folder: Path,
        events_folder: Path,
        output_folder: Path,
        logger: logging.Logger,
        api_key: str = "",  # Preserved for interface compatibility
    ) -> None:
        self.reference_folder = reference_folder
        self.events_folder = events_folder
        self.output_folder = output_folder
        self.logger = logger

        # Stores 128-dimensional face embedding vectors for students: {"Ljj": np.array([...]), ...}
        self._student_encodings: dict[str, np.ndarray] = {}
        self._student_names: list[str] = []

    # ------------------------------------------------------------------
    # Reference loading
    # ------------------------------------------------------------------

        def load_references(
        self,
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> list[str]:
        """Stage 1: Load reference photos and extract high-precision face embeddings using Multi-Jitter."""
        no_face_names: list[str] = []
        reference_images = sorted(
            p for p in self.reference_folder.iterdir() if is_image_file(p)
        )

        if not reference_images:
            self.logger.warning("No reference images found in %s", self.reference_folder)
            return no_face_names

        total = len(reference_images)
        for current, ref_path in enumerate(reference_images, start=1):
            student_name = ref_path.stem
            if progress_callback:
                progress_callback(current, total, student_name)

            try:
                rgb_image = self._load_and_resize(ref_path)

                # Primary detection using fast HOG; fallback to CNN if no face is detected
                locations = face_recognition.face_locations(rgb_image, model="hog")
                if not locations:
                    locations = face_recognition.face_locations(rgb_image, model="cnn")

                if not locations:
                    self.logger.warning("No face detected in reference photo for %s", student_name)
                    no_face_names.append(student_name)
                    continue

                # Extract face embeddings; num_jitters=5 computes an averaged, robust embedding vector
                encodings = face_recognition.face_encodings(
                    rgb_image, known_face_locations=locations, num_jitters=5
                )

                if encodings:
                    self._student_encodings[student_name] = encodings[0]
                    self._student_names.append(student_name)
                    self.logger.info("Loaded & Profiled 128D Embedding for: %s", student_name)
                else:
                    no_face_names.append(student_name)

            except Exception as exc:
                self.logger.error("Could not process reference %s: %s", ref_path.name, exc)
                no_face_names.append(student_name)

        return no_face_names

    # ------------------------------------------------------------------
    # Main sort loop
    # ------------------------------------------------------------------
 def sort_all(
        self,
        progress_callback: Callable[[int, int, str], None],
        cancelled: Callable[[], bool],
    ) -> dict[str, int]:
        """Stage 2: High-precision pipeline for comparison and classification."""
        images = collect_event_images(self.events_folder)
        total = len(images)
        counts = {"total": total, "matched": 0, "unmatched": 0, "skipped": 0}

        self.logger.info("Starting High-Precision Face Sorting - %d images found", total)

        for current, (image_path, event_name) in enumerate(images, start=1):
            if cancelled():
                self.logger.info("Sort cancelled by user at image %d/%d", current, total)
                break

            progress_callback(current, total, image_path.name)
            output_filename = build_output_filename(event_name, image_path.name)

            try:
                rgb_image = self._load_and_resize(image_path)
            except UnidentifiedImageError:
                self.logger.warning("Corrupted image, moving to _unmatched: %s", image_path.name)
                safe_copy(image_path, self.output_folder / "_unmatched", output_filename, self.logger)
                counts["unmatched"] += 1
                continue
            except Exception as exc:
                self.logger.error("Could not open %s: %s - skipping", image_path.name, exc)
                counts["skipped"] += 1
                continue

            try:
                # 1. Detect face locations (HOG fast detection with CNN fallback to prevent missed faces)
                face_locations = face_recognition.face_locations(rgb_image, model="hog")
                if not face_locations:
                    face_locations = face_recognition.face_locations(rgb_image, model="cnn")

                # 2. Extract 128D embedding vectors for all detected faces in the event photo
                face_encodings = face_recognition.face_encodings(rgb_image, face_locations)

                if not face_encodings:
                    self.logger.info("No face detected in %s -> _unmatched", image_path.name)
                    safe_copy(image_path, self.output_folder / "_unmatched", output_filename, self.logger)
                    counts["unmatched"] += 1
                    continue

                matched_students: set[str] = set()
                # 3. Compare Euclidean distance against reference embeddings
                for encoding in face_encodings:
                    match = self._match_face_embedding(encoding)
                    if match:
                        matched_students.add(match)

                # 4. Copy matched files to respective output directories
                if matched_students:
                    for student_name in matched_students:
                        dest_folder = self.output_folder / student_name
                        safe_copy(image_path, dest_folder, output_filename, self.logger)
                        self.logger.info("Matched %s -> %s", image_path.name, student_name)
                    counts["matched"] += 1
                else:
                    self.logger.info("No match (distance > %.2f): %s -> _unmatched", 
                                     self.DISTANCE_THRESHOLD, image_path.name)
                    safe_copy(image_path, self.output_folder / "_unmatched", output_filename, self.logger)
                    counts["unmatched"] += 1

            except Exception as exc:
                self.logger.error("Face detection failed for %s: %s", image_path.name, exc)
                safe_copy(image_path, self.output_folder / "_unmatched", output_filename, self.logger)
                counts["unmatched"] += 1

            finally:
                # Explicit garbage collection to prevent memory leaks
                if 'rgb_image' in locals():
                    del rgb_image
                gc.collect()

        return counts

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

   def _load_and_resize(self, image_path: Path) -> np.ndarray:
        """Reads an image, applies low-resource preprocessing, and returns a RGB NumPy array."""
        with Image.open(image_path) as img:
            img = img.convert("RGB")
            width, height = img.size
            longest = max(width, height)

            if longest > self.MAX_IMAGE_DIMENSION:
                scale = self.MAX_IMAGE_DIMENSION / longest
                new_size = (int(width * scale), int(height * scale))
                img = img.resize(new_size, Image.LANCZOS)

            return np.array(img)
    def _match_face_embedding(self, encoding: np.ndarray) -> str | None:
        """Calculates Euclidean distance and identifies the matching student based on threshold."""
        if not self._student_encodings:
            return None

        names = list(self._student_encodings.keys())
        known_encodings = np.array(list(self._student_encodings.values()))

        # Compute Euclidean distance vector
        distances = face_recognition.face_distance(known_encodings, encoding)
        best_idx = int(np.argmin(distances))
        best_distance = distances[best_idx]

        self.logger.debug(
            "Closest match: %s with distance: %.4f (Threshold: %.2f)",
            names[best_idx], best_distance, self.DISTANCE_THRESHOLD
        )

        # Classified as the same person if distance is below threshold
        if best_distance <= self.DISTANCE_THRESHOLD:
            return names[best_idx]

        return None

        names = list(self._student_encodings.keys())
        known_encodings = np.array(list(self._student_encodings.values()))

        distances = face_recognition.face_distance(known_encodings, encoding)
        best_idx = int(np.argmin(distances))
        best_distance = distances[best_idx]

        if best_distance <= self.DISTANCE_THRESHOLD:
            self.logger.debug(
                "Face matched to %s (distance=%.4f)", names[best_idx], best_distance
            )
            return names[best_idx]

        self.logger.debug("No match — best distance=%.4f", best_distance)
        return None
