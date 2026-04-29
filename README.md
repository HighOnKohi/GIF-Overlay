# GIF Overlay

A lightweight, floating transparent GIF and video overlay for your desktop.

## Features

- **Media Support**: Load GIF, MP4, AVI, MOV, WEBM, and MKV files and display them as a transparent overlay.
- **Background Removal**: 
  - Batch **chroma-key** background removal (fast, NumPy-accelerated).
  - Batch **AI background removal** via `rembg` (handles complex scenes).
- **Window Controls**: Drag to move around the screen, and drag edges or corners to resize (when unlocked).
- **Click-Through**: Lock the overlay to freeze its position and enable click-through (supported on Windows).
- **Playback Controls**: Smooth speed control from 0.1× to 5.0×.

## Dependencies

The project requires the following core dependencies:
- `PyQt6` (>= 6.5.0)
- `numpy` (>= 1.24.0)
- `Pillow` (>= 9.0.0)

There are also optional dependencies for additional features:
- `opencv-python`: Required for video file support (MP4, AVI, MOV, etc.).
- `rembg`: Required for AI-powered background removal (auto-downloads ~170 MB model).

You can install the core dependencies via pip:
```bash
pip install -r requirements.txt
```

Or install everything (including optional dependencies):
```bash
pip install PyQt6 numpy Pillow opencv-python rembg
```

## Usage

Run the main script to launch the application:
```bash
python gif_overlay.py
```
