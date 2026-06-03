"""
Model loader module for cell segmentation.

This module provides classes for loading and using different segmentation models:
  - Cellpose  (cyto, cyto2, nuclei, cpsam_finetuned) -- classic deep-learning cell segmenter
  - CellSAM                          -- SAM-based segmenter
  - InstanSeg                        -- winner of Cryobiology III; best F1 on nuclei images
                                        (+ custom TorchScript weights from Cryobiology 4)
  - StarDist 2D                      -- star-convex polygon segmenter (+ custom stardist0602)
  - YOLO11-seg                       -- ultralytics instance-seg (Cryobiology 4)

Quick-start examples
--------------------
    from cellsegkit import SegmenterFactory, run_segmentation

    seg = SegmenterFactory.create("instanseg")             # best for nuclei (fluorescence)
    seg = SegmenterFactory.create("cpsam_finetuned")       # Cryobiology 4 custom Cellpose-SAM
    seg = SegmenterFactory.create("instanseg_neuroblastoma")
    seg = SegmenterFactory.create("yolo11_512")            # Cryobiology 4 YOLO11x-seg

    run_segmentation(seg, input_dir="...", output_dir="...",
                     export_formats=("overlay", "npy", "png", "yolo"))

Model-type strings accepted by SegmenterFactory.create()
---------------------------------------------------------
    Built-in (no custom weights):
      "instanseg"              -> InstanSegSegmenter  (fluorescence_nuclei_and_cells)
      "instanseg:brightfield"  -> InstanSegSegmenter  (brightfield_nuclei)
      "stardist"               -> StarDistSegmenter   (2D_versatile_fluo)
      "stardist:he"            -> StarDistSegmenter   (2D_versatile_he)
      "stardist:dsb"           -> StarDistSegmenter   (2D_paper_dsb2018)
      "cyto"  / "cellpose"     -> CellposeSegmenter   (cyto)
      "cyto2"                  -> CellposeSegmenter   (cyto2)
      "nuclei"                 -> CellposeSegmenter   (nuclei)
      "cellsam"                -> CellSAMSegmenter

    Cryobiology 4 custom weights (expect files in WEIGHTS_DIR):
      "cpsam_finetuned"         -> CellposeSegmenter  (cpsam_finetuned.pth, diameter=40)
      "instanseg_neuroblastoma" -> InstanSegSegmenter (Instanseg-Neuroblastoma-v3.1.pt)
      "instanseg_0605"          -> InstanSegSegmenter (instanseg_20250605.pt)
      "yolo11_512"              -> YoloSegmenter      (YOLO11x-512-seg.pt)
      "yolo11_680"              -> YoloSegmenter      (YOLO11x-680-seg.pt)
      "yolo11_sphero"           -> YoloSegmenter      (YOLO11x-sphero-seg.pt)
      "stardist_0602"           -> StarDistSegmenter  (stardist0602/, opt-in, TF CPU-only)
"""

import os
import cv2
import numpy as np
import torch
from abc import ABC, abstractmethod
from pathlib import Path

from cellsegkit.utils.gpu_utils import get_device

# -- Cryobiology 4 weights location ------------------------------------------
# Resolution order:
#   1. env var CRYOBIOLOGY4_WEIGHTS (absolute path)
#   2. <repo_root>/cryobiology4/weights  (default after reorg v1.5.0)
# Path: shared/cellsegkit/cellsegkit/loader/model_loader.py
#   parents[0]=loader, [1]=cellsegkit(inner), [2]=cellsegkit(outer),
#   [3]=shared, [4]=repo_root
_DEFAULT_WEIGHTS_DIR = Path(__file__).resolve().parents[4] / "cryobiology4" / "weights"
WEIGHTS_DIR = Path(os.environ.get("CRYOBIOLOGY4_WEIGHTS", str(_DEFAULT_WEIGHTS_DIR)))


def _resolve_weight(filename: str) -> str:
    """Return absolute path to a weight file in WEIGHTS_DIR.
    No existence check — raised lazily by the model at load time with a clearer error."""
    return str(WEIGHTS_DIR / filename)

# -- Optional: Cellpose -------------------------------------------------------
try:
    from cellpose import models as cellpose_models
    CELLPOSE_AVAILABLE = True
except ImportError:
    CELLPOSE_AVAILABLE = False

# -- Optional: CellSAM --------------------------------------------------------
try:
    from cellSAM import segment_cellular_image
    CELLSAM_AVAILABLE = True
except ImportError:
    CELLSAM_AVAILABLE = False

# -- Optional: InstanSeg ------------------------------------------------------
try:
    from instanseg import InstanSeg as _InstanSegLib
    INSTANSEG_AVAILABLE = True
except (ImportError, Exception):
    # instanseg <0.1 has a different API (no InstanSeg class at top level)
    # upgrade with: pip install --upgrade instanseg
    INSTANSEG_AVAILABLE = False

# -- Optional: StarDist -------------------------------------------------------
try:
    from stardist.models import StarDist2D as _StarDist2D
    from csbdeep.utils import normalize as _csbdeep_normalize
    STARDIST_AVAILABLE = True
except (ImportError, RuntimeError):
    # stardist raises RuntimeError if TensorFlow is not installed
    # install with: pip install tensorflow stardist csbdeep
    STARDIST_AVAILABLE = False

# -- Optional: Ultralytics (YOLO11-seg) ---------------------------------------
try:
    from ultralytics import YOLO as _UltralyticsYOLO
    ULTRALYTICS_AVAILABLE = True
except ImportError:
    ULTRALYTICS_AVAILABLE = False


# -----------------------------------------------------------------------------
# Shared image-loading helper
# -----------------------------------------------------------------------------

def load_image_file(file_path, grayscale=False):
    """
    Load an image from a file path.

    Args:
        file_path: Path to the input image.
        grayscale: If True, return single-channel (H, W) array.
                   If False, return 3-channel RGB (H, W, 3) array.

    Returns:
        Loaded image as a numpy array.

    Raises:
        FileNotFoundError: If the image file does not exist.
        ValueError: If the image cannot be decoded.
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Image file '{file_path}' not found.")

    # cv2.imread() cannot handle Cyrillic / non-ASCII paths on Windows.
    # np.fromfile() reads raw bytes via Python (Unicode-safe), then
    # cv2.imdecode() decodes the in-memory buffer correctly.
    raw = np.fromfile(file_path, dtype=np.uint8)
    image = cv2.imdecode(raw, cv2.IMREAD_UNCHANGED)
    if image is None:
        raise ValueError(f"Failed to load image: {file_path}")

    if len(image.shape) == 2:               # already grayscale
        if not grayscale:
            image = cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)
    elif len(image.shape) == 3:
        if image.shape[2] > 3:              # drop alpha channel
            image = image[:, :, :3]
        if grayscale:
            image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        else:                               # OpenCV is BGR -> convert to RGB
            image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)

    return image


# -----------------------------------------------------------------------------
# Abstract base class
# -----------------------------------------------------------------------------

class BaseSegmenter(ABC):
    """
    Abstract base class for all segmenters.

    Subclasses must implement segment(image) -> int32 mask.
    load_image() has a default RGB implementation; override if needed.
    """

    def load_image(self, file_path):
        """Default: load as RGB numpy array."""
        return load_image_file(file_path, grayscale=False)

    @abstractmethod
    def segment(self, image):
        """
        Run segmentation on the provided image.

        Args:
            image: numpy array (H, W) or (H, W, C).

        Returns:
            Integer mask (H, W), dtype int32.
            Background = 0, each cell instance has a unique positive integer.
        """
        pass


# -----------------------------------------------------------------------------
# Factory
# -----------------------------------------------------------------------------

class SegmenterFactory:
    """Factory class for creating segmentation model instances."""

    _INSTANSEG_PRETRAINED = {
        "instanseg":             "fluorescence_nuclei_and_cells",
        "instanseg:fluo":        "fluorescence_nuclei_and_cells",
        "instanseg:brightfield": "brightfield_nuclei",
    }
    _STARDIST_PRETRAINED = {
        "stardist":      "2D_versatile_fluo",
        "stardist:fluo": "2D_versatile_fluo",
        "stardist:he":   "2D_versatile_he",
        "stardist:dsb":  "2D_paper_dsb2018",
    }
    _CELLPOSE_TYPES = {"cyto", "cyto2", "nuclei", "cellpose"}

    # Cryobiology 4 custom weights — file names inside WEIGHTS_DIR.
    _INSTANSEG_CUSTOM = {
        "instanseg_neuroblastoma": "Instanseg-Neuroblastoma-v3.1.pt",
        "instanseg_0605":          "instanseg_20250605.pt",
    }
    _CELLPOSE_CUSTOM = {
        # (weight_file, diameter) — diameter is the Cellpose scale hint.
        "cpsam_finetuned": ("cpsam_finetuned.pth", 40),
    }
    _YOLO_CUSTOM = {
        "yolo11_512":    "YOLO11x-512-seg.pt",
        "yolo11_680":    "YOLO11x-680-seg.pt",
        "yolo11_sphero": "YOLO11x-sphero-seg.pt",
    }
    _STARDIST_CUSTOM = {
        # Opt-in: TF on Windows = CPU-only = ~26h/154 imgs.
        # NOT added to run_segmentation.py ALL_MODELS.
        "stardist_0602": "stardist0602",
    }

    @staticmethod
    def create(model_type: str, use_gpu: bool = True, sam_checkpoint_path=None):
        """
        Instantiate the correct segmenter based on model_type string.

        Args:
            model_type:  See module docstring for accepted values.
            use_gpu:     Use GPU if available.
            sam_checkpoint_path: SAM checkpoint path (CellSAM only).

        Returns:
            BaseSegmenter subclass instance.

        Raises:
            ValueError:  Unknown model_type.
            ImportError: Required package not installed.
        """
        key = model_type.strip().lower()

        # InstanSeg built-in
        if key in SegmenterFactory._INSTANSEG_PRETRAINED:
            if not INSTANSEG_AVAILABLE:
                raise ImportError(
                    "InstanSeg is not installed.\n"
                    "Install: pip install instanseg-torch"
                )
            pretrained = SegmenterFactory._INSTANSEG_PRETRAINED[key]
            return InstanSegSegmenter(pretrained_model=pretrained, use_gpu=use_gpu)

        # InstanSeg Cryobiology 4 custom (TorchScript weights)
        if key in SegmenterFactory._INSTANSEG_CUSTOM:
            if not INSTANSEG_AVAILABLE:
                raise ImportError(
                    "InstanSeg is not installed.\n"
                    "Install: pip install instanseg-torch"
                )
            fname = SegmenterFactory._INSTANSEG_CUSTOM[key]
            return InstanSegSegmenter(
                pretrained_model=_resolve_weight(fname),
                use_gpu=use_gpu,
                from_torchscript=True,
            )

        # StarDist (built-in aliases)
        if key in SegmenterFactory._STARDIST_PRETRAINED:
            if not STARDIST_AVAILABLE:
                raise ImportError(
                    "StarDist is not installed.\n"
                    "Install: pip install stardist csbdeep"
                )
            pretrained = SegmenterFactory._STARDIST_PRETRAINED[key]
            return StarDistSegmenter(pretrained_model=pretrained, use_gpu=use_gpu)

        # StarDist Cryobiology 4 custom alias (opt-in, TF CPU-only on Windows)
        if key in SegmenterFactory._STARDIST_CUSTOM:
            if not STARDIST_AVAILABLE:
                raise ImportError(
                    "StarDist is not installed (TensorFlow missing).\n"
                    "Install: pip install tensorflow stardist csbdeep\n"
                    "Note: Windows TF has no GPU support - CPU ~26h/154 imgs."
                )
            folder = SegmenterFactory._STARDIST_CUSTOM[key]
            return StarDistSegmenter(
                pretrained_model=_resolve_weight(folder), use_gpu=use_gpu
            )

        # StarDist (custom path: "stardist:/path/to/model")
        if key.startswith("stardist:"):
            custom_path = model_type[len("stardist:"):]
            if not STARDIST_AVAILABLE:
                raise ImportError(
                    "StarDist is not installed.\n"
                    "Install: pip install stardist csbdeep"
                )
            return StarDistSegmenter(pretrained_model=custom_path, use_gpu=use_gpu)

        # Cellpose built-in family
        if key in SegmenterFactory._CELLPOSE_TYPES:
            if not CELLPOSE_AVAILABLE:
                raise ImportError(
                    "Cellpose is not installed.\n"
                    "Install: pip install cellpose"
                )
            cp_type = key if key != "cellpose" else "cyto"
            return CellposeSegmenter(model_type=cp_type, use_gpu=use_gpu)

        # Cellpose Cryobiology 4 custom (cpsam_finetuned)
        if key in SegmenterFactory._CELLPOSE_CUSTOM:
            if not CELLPOSE_AVAILABLE:
                raise ImportError(
                    "Cellpose is not installed.\n"
                    "Install: pip install cellpose"
                )
            fname, diameter = SegmenterFactory._CELLPOSE_CUSTOM[key]
            return CellposeSegmenter(
                pretrained_model=_resolve_weight(fname),
                diameter=diameter,
                use_gpu=use_gpu,
            )

        # YOLO11-seg Cryobiology 4 custom
        if key in SegmenterFactory._YOLO_CUSTOM:
            if not ULTRALYTICS_AVAILABLE:
                raise ImportError(
                    "ultralytics is not installed.\n"
                    "Install: pip install ultralytics"
                )
            fname = SegmenterFactory._YOLO_CUSTOM[key]
            return YoloSegmenter(weights_path=_resolve_weight(fname), use_gpu=use_gpu)

        # CellSAM
        if key == "cellsam":
            if not CELLSAM_AVAILABLE:
                raise ImportError(
                    "cellSAM is not installed.\n"
                    "Install: pip install 'cellsegkit[cellsam]'"
                )
            return CellSAMSegmenter(use_gpu=use_gpu)

        raise ValueError(
            f"Unknown model_type: '{model_type}'.\n"
            "Valid options:\n"
            "  built-in: instanseg, instanseg:brightfield, stardist, stardist:he,\n"
            "            stardist:dsb, cyto, cyto2, nuclei, cellpose, cellsam\n"
            "  Cryobiology 4: cpsam_finetuned, instanseg_neuroblastoma, instanseg_0605,\n"
            "                 yolo11_512, yolo11_680, yolo11_sphero,\n"
            "                 stardist_0602 (opt-in, TF CPU-only)"
        )


# -----------------------------------------------------------------------------
# InstanSeg  (winner of Cryobiology III -- best Precision/Recall/F1)
# Reference: kikuroki/Cells-calculator -> model/InstanSegSegmenter.py
# Paper: Goldsborough et al., 2024 (https://arxiv.org/abs/2408.12786)
# -----------------------------------------------------------------------------

class InstanSegSegmenter(BaseSegmenter):
    """
    Cell segmenter based on InstanSeg.

    Pretrained models:
      "fluorescence_nuclei_and_cells"  (default) -- for fluorescence images
      "brightfield_nuclei"                        -- for brightfield microscopy
    """

    BUILTIN_MODELS = (
        "fluorescence_nuclei_and_cells",
        "brightfield_nuclei",
    )

    def __init__(
        self,
        pretrained_model="fluorescence_nuclei_and_cells",
        use_gpu=True,
        from_torchscript=False,
    ):
        if not INSTANSEG_AVAILABLE:
            raise ImportError("InstanSeg is not installed. Run: pip install instanseg-torch")

        self.pretrained_model = pretrained_model
        self.device = get_device(prefer_gpu=use_gpu)

        if from_torchscript:
            # Cryobiology 4 custom weights: reference code does
            #   module = torch.jit.load(path); model = InstanSeg(module, verbosity=1)
            if not os.path.exists(pretrained_model):
                raise FileNotFoundError(
                    f"InstanSeg TorchScript weight not found: {pretrained_model}"
                )
            # torch.jit.load uses C-level fopen which can't handle Cyrillic
            # Windows paths. Route through a Python file handle instead.
            import io
            with open(pretrained_model, "rb") as f:
                buffer = io.BytesIO(f.read())
            module = torch.jit.load(buffer, map_location=str(self.device))
            try:
                self.model = _InstanSegLib(module, device=str(self.device), verbosity=1)
            except TypeError:
                self.model = _InstanSegLib(module, verbosity=1)
                if hasattr(self.model, "to"):
                    self.model = self.model.to(self.device)
            print(
                f"[InstanSegSegmenter] TorchScript={pretrained_model!r}  device={self.device}"
            )
            return

        model_name = pretrained_model if pretrained_model in self.BUILTIN_MODELS \
                                       else "fluorescence_nuclei_and_cells"

        # instanseg-torch 0.1.x: принимает device в конструкторе, .to() у объекта нет.
        # instanseg <0.1: нет параметра device, переносится через .to() как nn.Module.
        try:
            self.model = _InstanSegLib(model_name, device=str(self.device), verbosity=1)
        except TypeError:
            # Старый API — без device в конструкторе
            self.model = _InstanSegLib(model_name, verbosity=1)
            if hasattr(self.model, "to"):
                self.model = self.model.to(self.device)

        print(f"[InstanSegSegmenter] model={pretrained_model!r}  device={self.device}")

    def load_image(self, file_path):
        """Load as RGB (required by InstanSeg)."""
        return load_image_file(file_path, grayscale=False)

    def segment(self, image):
        """
        Segment cells. Returns int32 mask (H, W).
        Старий API повертав tensor (1, 1, H, W). Новий instanseg-torch 0.1.x
        повертає (labeled_output, image_tensor) і сам labeled — (1, C, H, W).
        """
        result = self.model.eval_medium_image(
            image=image,
            return_image_tensor=False,
            target="cells",
        )

        # Новий API може повертати кортеж/список (labels, image) — беремо перший
        if isinstance(result, (tuple, list)):
            labeled = result[0]
        else:
            labeled = result

        # labeled shape: (1, C, H, W) або (1, 1, H, W). Беремо останній канал.
        if hasattr(labeled, "detach"):      # torch.Tensor
            labeled = labeled.detach().cpu().numpy()
        arr = np.asarray(labeled)
        while arr.ndim > 2:
            arr = arr[0]                     # поступово зрізаємо провідні осі
        return arr.astype(np.int32)


# -----------------------------------------------------------------------------
# StarDist 2D  (runner-up in Cryobiology III)
# Reference: kikuroki/Cells-calculator -> model/StardistSegmenter.py
# Paper: Weigert et al., 2020 (https://arxiv.org/abs/2006.14673)
# -----------------------------------------------------------------------------

class StarDistSegmenter(BaseSegmenter):
    """
    Cell segmenter based on StarDist 2D (star-convex polygon representation).

    Pretrained models:
      "2D_versatile_fluo"   (default) -- fluorescence, single-channel input
      "2D_versatile_he"               -- H&E stained tissue, RGB input
      "2D_paper_dsb2018"              -- DSB 2018 nuclei dataset
    """

    BUILTIN_MODELS = (
        "2D_versatile_fluo",
        "2D_versatile_he",
        "2D_paper_dsb2018",
    )
    _RGB_MODELS = {"2D_versatile_he"}

    def __init__(self, pretrained_model="2D_versatile_fluo", use_gpu=True):
        if not STARDIST_AVAILABLE:
            raise ImportError(
                "StarDist is not installed. Run: pip install stardist csbdeep"
            )

        self.pretrained_model = pretrained_model
        self.device = get_device(prefer_gpu=use_gpu)

        if pretrained_model in self.BUILTIN_MODELS:
            self.model = _StarDist2D.from_pretrained(pretrained_model)
        else:
            basedir = os.path.dirname(pretrained_model)
            name    = os.path.basename(pretrained_model)
            self.model = _StarDist2D(None, name=name, basedir=basedir)

        self._needs_rgb = pretrained_model in self._RGB_MODELS
        input_fmt = "RGB" if self._needs_rgb else "grayscale"
        print(f"[StarDistSegmenter] model={pretrained_model!r}  input={input_fmt}  device={self.device}")

    def load_image(self, file_path):
        """Load as RGB for H&E models, grayscale for fluorescence/DSB models."""
        return load_image_file(file_path, grayscale=not self._needs_rgb)

    def segment(self, image):
        """
        Normalize (1st-99.8th percentile) then predict.
        Returns int32 mask (H, W).
        """
        img_norm = _csbdeep_normalize(image, 1, 99.8)
        labels, _details = self.model.predict_instances(img_norm)
        return labels.astype(np.int32)


# -----------------------------------------------------------------------------
# Cellpose  (cyto / cyto2 / nuclei)
# -----------------------------------------------------------------------------

class CellposeSegmenter(BaseSegmenter):
    """
    Cell segmenter using Cellpose.

    Model types:
      "cyto"   -- whole cells (cytoplasm visible, no cell walls)
      "cyto2"  -- improved cyto; generally preferred
      "nuclei" -- cell nuclei with visible boundaries
    """

    def __init__(
        self,
        model_type="cyto",
        use_gpu=True,
        pretrained_model=None,
        diameter=None,
    ):
        if not CELLPOSE_AVAILABLE:
            raise ImportError("Cellpose is not installed. Run: pip install cellpose")

        self.model_type = model_type
        self.diameter = diameter  # may be None = let Cellpose auto-estimate
        self.device = get_device(prefer_gpu=use_gpu)
        self.use_gpu = use_gpu and (str(self.device) == "cuda")

        if pretrained_model is not None:
            # Cryobiology 4: custom finetuned weights (cpsam_finetuned.pth etc.)
            if not os.path.exists(pretrained_model):
                raise FileNotFoundError(
                    f"Cellpose custom weight not found: {pretrained_model}"
                )
            self.pretrained_model = pretrained_model
            self.model = cellpose_models.CellposeModel(
                gpu=self.use_gpu, pretrained_model=pretrained_model
            )
            print(
                f"[CellposeSegmenter] custom={pretrained_model!r}  "
                f"diameter={diameter}  device={self.device}"
            )
        else:
            self.pretrained_model = None
            self.model = cellpose_models.CellposeModel(
                gpu=self.use_gpu, model_type=self.model_type
            )
            print(f"[CellposeSegmenter] model={model_type!r}  device={self.device}")

    def load_image(self, file_path):
        """Load as RGB."""
        return load_image_file(file_path, grayscale=False)

    def segment(self, image):
        """Run Cellpose eval. Returns int32 mask (H, W)."""
        out = self.model.eval(image, diameter=self.diameter, channels=[0, 0])
        # Cellpose returns (masks, flows, styles) in <4.x, (masks, flows, styles, diams) in some.
        masks = out[0]
        return masks.astype(np.int32)


# -----------------------------------------------------------------------------
# CellSAM
# -----------------------------------------------------------------------------

class CellSAMSegmenter(BaseSegmenter):
    """CellSAM segmenter. Requires grayscale input."""

    def __init__(self, use_gpu=True):
        if not CELLSAM_AVAILABLE:
            raise ImportError(
                "cellSAM is not installed. Run: pip install 'cellsegkit[cellsam]'"
            )
        self.device = get_device(prefer_gpu=use_gpu)
        self.device_str = str(self.device)
        print(f"[CellSAMSegmenter] device={self.device}")

    def load_image(self, file_path):
        """Load as grayscale (required by CellSAM)."""
        return load_image_file(file_path, grayscale=True)

    def segment(self, image):
        """Run CellSAM. Returns int32 mask (H, W)."""
        if not CELLSAM_AVAILABLE:
            raise ImportError(
                "cellSAM is not installed. Run: pip install 'cellsegkit[cellsam]'"
            )
        mask, _, _ = segment_cellular_image(image, device=self.device_str)
        return mask.astype(np.int32)


# -----------------------------------------------------------------------------
# YOLO11-seg (Cryobiology 4 custom weights)
# Reference: v3.1 Cells-Calculator/model/YOLOSegmenter.py
# -----------------------------------------------------------------------------

class YoloSegmenter(BaseSegmenter):
    """
    Instance-segmentation wrapper around ultralytics.YOLO (YOLO11-seg).

    Cryobiology 4 weights:
      YOLO11x-512-seg.pt    — L929 monolayer (512-trained)
      YOLO11x-680-seg.pt    — L929 monolayer (680-trained, Full)
      YOLO11x-sphero-seg.pt — spheroids / spherical MSCs
    """

    def __init__(self, weights_path: str, use_gpu: bool = True):
        if not ULTRALYTICS_AVAILABLE:
            raise ImportError(
                "ultralytics is not installed. Run: pip install ultralytics"
            )
        if not os.path.exists(weights_path):
            raise FileNotFoundError(f"YOLO weights not found: {weights_path}")

        self.weights_path = weights_path
        self.device = get_device(prefer_gpu=use_gpu)
        # ultralytics.YOLO accepts task="segment" to force seg head wiring
        self.model = _UltralyticsYOLO(weights_path, task="segment")
        # Ultralytics handles device via .to() or per-call device=...
        try:
            self.model.to(str(self.device))
        except Exception:
            pass
        print(f"[YoloSegmenter] weights={os.path.basename(weights_path)!r}  device={self.device}")

    def load_image(self, file_path):
        """Load as RGB (YOLO expects numpy HWC or path)."""
        return load_image_file(file_path, grayscale=False)

    def segment(self, image):
        """
        Run YOLO11-seg inference. Returns int32 mask (H, W).

        Reference v3.1 kwargs: conf=0.3, iou=0.6, max_det=2000, retina_masks=True.
        Each detection's binary mask is painted with a unique instance id.
        """
        results = self.model(
            image,
            conf=0.3,
            iou=0.6,
            max_det=2000,
            retina_masks=True,
            verbose=False,
            device=str(self.device),
        )
        result = results[0]
        h, w = image.shape[:2]
        mask = np.zeros((h, w), dtype=np.int32)

        if result.masks is None or len(result.masks) == 0:
            return mask

        # result.masks.data: (N, Hm, Wm) torch tensor of 0/1 floats
        raw = result.masks.data.detach().cpu().numpy()
        for i in range(raw.shape[0]):
            m = raw[i]
            if m.shape != (h, w):
                # retina_masks=True usually returns original-res masks; resize if not
                m = cv2.resize(m.astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST)
            mask[m > 0.5] = i + 1
        return mask
