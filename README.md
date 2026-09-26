# qr_transfer

Transfer an arbitrary file to an air-gapped machine by displaying it as a
sequence of QR codes on screen, filming that with a phone camera, and
decoding the recorded video back into the original file.

```
source machine                         phone                    destination machine
┌───────────────┐   flashes QR codes   ┌─────┐   video file    ┌───────────────┐
│ encode <file> │ ───────────────────▶ │ cam │ ──────────────▶ │ decode <video>│
└───────────────┘                      └─────┘                 └───────────────┘
```

## How it works

- The input file is split into small chunks (default 1200 bytes each).
- Each chunk gets a small binary header (magic bytes, frame index, total
  frame count, frame type, CRC32 of the payload), is base64-encoded, and
  rendered as a QR code.
- A special "frame 0" carries JSON metadata: original filename, file size,
  chunk size, total chunk count, and a SHA256 hash of the whole file.
- The encoder cycles through all frames in a window, each shown for a fixed
  duration (default 200 ms, i.e. ~5 QR codes/sec).
- You film the window with a phone camera. Because each frame is shown for
  significantly longer than one camera frame interval, the camera will
  typically capture each QR code several times in a row - this redundancy is
  what makes the pipeline robust to timing mismatches between the display and
  the camera.
- After transferring the video to the destination machine, the decoder scans
  every video frame with a QR reader (pyzbar/zbar), deduplicates repeated
  captures by frame index, verifies each chunk's CRC32, and once all chunks
  are present, verifies the whole-file SHA256 and writes the reconstructed
  file to disk.
- If any chunks are missing (frame flashed too fast, camera focus hiccup,
  etc.), the decoder reports exactly which chunk indices are missing and
  aborts without writing a (possibly corrupt) file. Re-record and try again,
  e.g. with a slower `--duration-ms`.

Binary payloads are base64-encoded before being put into the QR code. This
is required because `zbar` (the QR decoding library) performs its own
internal text-encoding detection on decoded byte-mode QR data, which can
silently corrupt arbitrary binary bytes. Base64 keeps the QR payload to a
safe printable-ASCII subset that survives that round trip byte-for-byte, at
the cost of ~33% size overhead.

## Requirements

- Python 3.9+
- A system with a display (the encoder opens a window with OpenCV)

Install dependencies:

```bash
pip install -r requirements.txt
```

`pyzbar` wheels bundle the required `zbar` shared library on Windows and
macOS. On Linux you additionally need the system package:

```bash
sudo apt install libzbar0
```

## Usage

### Encode (source machine)

```bash
python qr_transfer.py encode path/to/file.zip
```

This opens a window showing "press ENTER to begin" so you have time to
position and start recording on your phone. Press **Enter** in the window to
start (or `q`/`Esc` to cancel). An optional countdown then plays before the
QR frames start cycling.

Options:

| Flag | Default | Description |
|---|---|---|
| `--duration-ms` | `200` | Milliseconds each QR frame is shown. Lower = faster transfer, higher = more tolerant of slower/older phone cameras. |
| `--chunk-size` | `1200` | Bytes of file data packed into each QR frame (before base64). Larger = fewer frames but denser/harder-to-scan QR codes. |
| `--window-size` | `800` | Width/height in pixels of the display window. |
| `--countdown` | `5` | Seconds of numeric countdown shown after pressing Enter, before frames start. `0` disables it. |
| `--no-wait` | off | Skip the "press ENTER to begin" screen entirely and go straight to the countdown/frames. |

Record the window with your phone's camera for the full duration (terminal
shows progress and a completion message when done). Press `q`/`Esc` at any
time during playback to abort.

Transfer the resulting video file to the destination machine (e.g. via USB
drive, SD card, etc. - however you'd normally move files across an air gap).

### Decode (destination machine)

```bash
python qr_transfer.py decode recording.mp4
```

Options:

| Flag | Default | Description |
|---|---|---|
| `-o`, `--output` | embedded filename | Output file path. If omitted, uses the original filename embedded in the QR metadata, written to the current directory. |
| `--force` | off | Overwrite the output file if it already exists. |

The decoder scans every frame of the video, prints progress as chunks are
found, and on success writes the reconstructed file after verifying its
SHA256 hash matches the original.

If chunks are missing, it prints the missing chunk indices and exits with an
error instead of writing a partial/corrupt file - re-record (e.g. hold the
phone steadier, improve lighting, or increase `--duration-ms` on the encode
side) and decode again.

#### Supported video formats

`decode` opens the video via OpenCV, which uses a bundled FFmpeg build under
the hood, so most common containers/codecs work out of the box: MP4/MOV/MKV/
AVI/WebM with H.264, H.265/HEVC, VP8/VP9, MJPEG, etc. - including typical
iPhone and Android camera recordings.

If a video fails to open, re-encode it to plain H.264 MP4 first:

```bash
ffmpeg -i recording.mov -c:v libx264 -crf 18 recording_h264.mp4
```

then decode the converted file.

## Notes and limitations

- No compression or encryption is applied - this is a transport mechanism,
  not a security tool. Pre-compress/encrypt the file yourself first if
  needed.
- The encoder does a single pass through all frames; there's no built-in
  "resume" mode for missing chunks - if a chunk is missing, re-record the
  whole sequence.
- Very large files result in many frames and a longer recording. Increase
  `--chunk-size` to reduce frame count (bigger/denser QR codes) or accept a
  longer recording, depending on your phone camera's ability to resolve
  finer QR modules.
