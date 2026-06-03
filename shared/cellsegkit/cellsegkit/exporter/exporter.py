"""
Exporter module for saving segmentation results.

This module provides functions for exporting segmentation masks in various formats,
including numpy arrays, PNG images, YOLO annotations, and visual overlays.
"""

import numpy as np
from PIL import Image
import os
import cv2
from pathlib import Path
from typing import Callable, Iterable, Tuple
from skimage.segmentation import find_boundaries


def save_mask_as_npy(mask: np.ndarray, output_path: str, silent: bool = False) -> bool:
    """
    Saves the segmentation mask as a .npy file.

    Args:
        mask: Input mask as a numpy array
        output_path: Path where the .npy file will be saved
        silent: If True, suppresses success messages (default: False)

    Returns:
        bool: True if successful, False otherwise
    """
    try:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        np.save(output_path, mask)
        if not silent:
            print(f"✅ Mask saved as .npy: {output_path}")
        return True
    except Exception as e:
        print(f"❌ Failed to save mask as .npy: {e}")
        return False


def save_mask_as_png(mask: np.ndarray, output_path: str, silent: bool = False) -> bool:
    """
    Saves the segmentation mask as a 16-bit PNG file (instance IDs 0..65535).

    Pre-2026-05-13 used mode="P" (8-bit indexed) — silently wrapped any
    instance ID > 255 to 0..255, corrupting data on dense images (e.g.,
    2000+ vesicles via multi-seed). Now writes 16-bit grayscale PNG:
    readers using ``np.array(Image.open(path))`` auto-detect uint16 dtype.

    Args:
        mask: Input mask with integer labels as a numpy array
        output_path: Path where the PNG file will be saved
        silent: If True, suppresses success messages (default: False)

    Returns:
        bool: True if successful, False otherwise
    """
    try:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        max_id = int(mask.max()) if mask.size else 0
        if max_id > 65535:
            print(
                f"⚠️  Mask has max ID {max_id} but PNG uint16 supports only 65535; "
                "values will wrap. Use .npy for full int32 precision."
            )
        mask_img = Image.fromarray(mask.astype(np.uint16))  # PIL auto-detects → mode 'I;16'
        mask_img.save(output_path, format="PNG")
        if not silent:
            print(f"✅ Mask saved as PNG: {output_path}")
        return True
    except Exception as e:
        print(f"❌ Failed to save mask as PNG: {e}")
        return False


def export_yolo_annotations(
    mask: np.ndarray,
    output_txt_path: str,
    image_size: Tuple[int, int],
    class_id: int = 0,
    silent: bool = False,
) -> bool:
    """
    Converts the segmentation mask into YOLO-format bounding boxes and saves to a .txt file.

    Args:
        mask: Input mask with integer labels as a numpy array
        output_txt_path: Path where the annotations .txt will be saved
        image_size: Tuple as (width, height) of the original image
        class_id: Class ID to assign to all bounding boxes. Default is 0
        silent: If True, suppresses success messages (default: False)

    Returns:
        bool: True if successful, False otherwise
    """
    try:
        os.makedirs(os.path.dirname(output_txt_path), exist_ok=True)
        width, height = image_size
        annotations = []

        # Find unique object IDs in the mask (excluding background, label 0)
        for obj_id in np.unique(mask):
            if obj_id == 0:
                continue

            # Extract coordinates for the current object
            object_coords = np.argwhere(mask == obj_id)
            y_min, x_min = object_coords.min(axis=0)
            y_max, x_max = object_coords.max(axis=0)

            # Convert to YOLO format (normalized center_x, center_y, width, height)
            center_x = ((x_min + x_max) / 2) / width
            center_y = ((y_min + y_max) / 2) / height
            bbox_width = (x_max - x_min) / width
            bbox_height = (y_max - y_min) / height

            # Write annotation: class_id, center_x, center_y, bbox_width, bbox_height
            annotations.append(
                f"{class_id} {center_x:.6f} {center_y:.6f} {bbox_width:.6f} {bbox_height:.6f}"
            )

        # Save annotations to file
        with open(output_txt_path, "w") as f:
            f.write("\n".join(annotations))

        if not silent:
            print(f"✅ Annotations saved to {output_txt_path}")
        return True
    except Exception as e:
        print(f"❌ Failed to export YOLO annotations: {e}")
        return False


def draw_overlay(
    image: np.ndarray, mask: np.ndarray, output_path: str, silent: bool = False
) -> bool:
    """
    Draw boundaries on top of the image and save as an overlay PNG.

    Pre-2026-05-13 used `plt.savefig(..., bbox_inches="tight")` з figsize=(10,10):
    matplotlib downsamples original image to 1000×1000 px і повний save bere
    ~1-3s per file. Replaced with direct PIL write — зберігає original
    resolution та працює ~30-50x швидше (Image.fromarray + save: 10-100ms).

    Args:
        image: Original image (grayscale or RGB) as numpy array
        mask: Segmentation mask (labels)
        output_path: Path to save overlay result
        silent: If True, suppresses success messages (default: False)

    Returns:
        bool: True if successful, False otherwise
    """
    try:
        boundaries = find_boundaries(mask, mode="outer")

        # Handle grayscale → RGB conversion
        if len(image.shape) == 2:
            overlaid = cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)
        elif image.shape[2] == 1:
            overlaid = cv2.cvtColor(image[:, :, 0], cv2.COLOR_GRAY2RGB)
        else:
            overlaid = image.copy()

        # Apply red boundaries (RGB)
        overlaid[boundaries] = [255, 0, 0]

        # PIL потребує uint8 RGB
        if overlaid.dtype != np.uint8:
            overlaid = overlaid.astype(np.uint8)

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        Image.fromarray(overlaid, mode="RGB").save(output_path, format="PNG")

        if not silent:
            print(f"✅ Overlay saved: {output_path}")
        return True
    except Exception as e:
        print(f"❌ Failed to save overlay: {e}")
        return False


VALID_EXPORT_FORMATS = {"overlay", "npy", "png", "yolo"}


def export_segmentation_bundle(
    mask: np.ndarray,
    output_dir: str | os.PathLike,
    stem: str,
    image: np.ndarray | None = None,
    export_formats: Iterable[str] = ("overlay", "npy", "png", "yolo"),
    image_size: Tuple[int, int] | None = None,
    class_id: int = 0,
    yolo_writer: Callable[[np.ndarray, str, Tuple[int, int], bool], bool] | None = None,
    silent: bool = False,
) -> dict:
    """
    Export one segmentation mask into the project-standard format bundle.

    This is the shared v1.7 render path used by both batch segmentation and
    Mask Picker baking. It intentionally delegates to the older single-format
    helpers above, so existing pixel/YOLO behavior stays stable while callers
    stop duplicating path and format logic.

    Args:
        mask: Integer instance mask, background=0.
        output_dir: Model output folder that will receive {overlay,npy,png,yolo}/.
        stem: File stem used for every exported artifact.
        image: Original image array, required for overlay output.
        export_formats: Any subset of overlay/npy/png/yolo.
        image_size: Optional (width, height). If omitted and image is present,
            it is inferred from image.shape.
        class_id: Class id for the default YOLO exporter.
        yolo_writer: Optional custom writer for multiclass Mask Picker baking.
            Signature: (mask, output_txt_path, image_size, silent) -> bool.
        silent: Forwarded to low-level exporters.

    Returns:
        {"files": {"npy": "...", ...}, "errors": ["overlay", ...]}
    """
    formats = tuple(export_formats)
    invalid = set(formats) - VALID_EXPORT_FORMATS
    if invalid:
        raise ValueError(
            f"Invalid export format(s): {', '.join(sorted(invalid))}. "
            f"Valid formats are: {', '.join(sorted(VALID_EXPORT_FORMATS))}"
        )

    out_root = Path(output_dir)
    files: dict[str, str] = {}
    errors: list[str] = []

    if image_size is None and image is not None:
        image_height, image_width = image.shape[:2]
        image_size = (int(image_width), int(image_height))

    if "overlay" in formats:
        overlay_path = out_root / "overlay" / f"{stem}.png"
        if image is None:
            errors.append("overlay")
        elif draw_overlay(image, mask, str(overlay_path), silent=silent):
            files["overlay"] = str(overlay_path)
        else:
            errors.append("overlay")

    if "npy" in formats:
        npy_path = out_root / "npy" / f"{stem}.npy"
        if save_mask_as_npy(mask, str(npy_path), silent=silent):
            files["npy"] = str(npy_path)
        else:
            errors.append("npy")

    if "png" in formats:
        png_path = out_root / "png" / f"{stem}.png"
        if save_mask_as_png(mask, str(png_path), silent=silent):
            files["png"] = str(png_path)
        else:
            errors.append("png")

    if "yolo" in formats:
        yolo_path = out_root / "yolo" / f"{stem}.txt"
        if image_size is None:
            errors.append("yolo")
        else:
            writer = yolo_writer
            ok = (
                writer(mask, str(yolo_path), image_size, silent)
                if writer is not None
                else export_yolo_annotations(
                    mask, str(yolo_path), image_size, class_id=class_id, silent=silent
                )
            )
            if ok:
                files["yolo"] = str(yolo_path)
            else:
                errors.append("yolo")

    return {"files": files, "errors": errors}
