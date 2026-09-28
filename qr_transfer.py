#!/usr/bin/env python3
"""
qr_transfer.py

Encode an arbitrary file into a sequence of QR codes shown one-by-one in a
window (film it with a phone camera), and decode a video recording of that
sequence back into the original file on another machine.

Usage:
    python qr_transfer.py encode <input_file> [--duration-ms 200] [--chunk-size 1000]
                                                [--window-size 800] [--countdown 5]
                                                [--no-wait] [--info]
    python qr_transfer.py decode <video_file> [-o OUTPUT] [--force] [--decoder opencv]
"""
from __future__ import annotations

import argparse
import base64
import json
import struct
import sys
import time
import zlib
import hashlib
from pathlib import Path

import numpy as np
import cv2
import qrcode
from qrcode.constants import ERROR_CORRECT_M
from qrcode.util import QRData, MODE_8BIT_BYTE
from PIL import Image

MAGIC = b"QRV1"
FRAME_TYPE_META = 0
FRAME_TYPE_DATA = 1

# magic(4s) frame_index(I) total_frames(I) frame_type(B) payload_crc32(I)
HEADER_FMT = ">4sIIBI"
HEADER_SIZE = struct.calcsize(HEADER_FMT)


# --------------------------------------------------------------------------
# Framing helpers
# --------------------------------------------------------------------------

def pack_frame(frame_index: int, total_frames: int, frame_type: int, payload: bytes) -> bytes:
    crc = zlib.crc32(payload) & 0xFFFFFFFF
    header = struct.pack(HEADER_FMT, MAGIC, frame_index, total_frames, frame_type, crc)
    return header + payload


def unpack_frame(raw: bytes):
    if len(raw) < HEADER_SIZE:
        return None
    header = raw[:HEADER_SIZE]
    payload = raw[HEADER_SIZE:]
    magic, frame_index, total_frames, frame_type, crc = struct.unpack(HEADER_FMT, header)
    if magic != MAGIC:
        return None
    if (zlib.crc32(payload) & 0xFFFFFFFF) != crc:
        return None
    return {
        "frame_index": frame_index,
        "total_frames": total_frames,
        "frame_type": frame_type,
        "payload": payload,
    }


# --------------------------------------------------------------------------
# Encode
# --------------------------------------------------------------------------

def make_qr_image(data: bytes, window_size: int) -> np.ndarray:
    # The QR decoder used on the decode side (OpenCV's QRCodeDetector)
    # applies its own text-encoding guessing to byte-mode QR payloads and can
    # silently corrupt arbitrary binary data as a result. Base64-encoding the
    # payload first keeps the QR content to a safe printable-ASCII subset
    # that survives that round trip intact, at the cost of ~33% size overhead.
    b64 = base64.b64encode(data)
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, border=4)
    qr.add_data(QRData(b64, mode=MODE_8BIT_BYTE, check_data=False))
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("L")
    img = img.resize((window_size, window_size), Image.NEAREST)
    return np.array(img)


def countdown_screen(window_size: int, seconds: int, window_name: str):
    for remaining in range(seconds, 0, -1):
        canvas = np.full((window_size, window_size), 255, dtype=np.uint8)
        text = str(remaining)
        font = cv2.FONT_HERSHEY_SIMPLEX
        (tw, th), _ = cv2.getTextSize(text, font, 6, 10)
        org = ((window_size - tw) // 2, (window_size + th) // 2)
        cv2.putText(canvas, text, org, font, 6, (0,), 10, cv2.LINE_AA)
        cv2.imshow(window_name, canvas)
        cv2.waitKey(1000)


def wait_for_enter_screen(window_size: int, window_name: str) -> bool:
    """Show a 'get ready' screen and block until the user presses ENTER.

    Returns False if the user aborted (q/ESC), True once ENTER was pressed.
    """
    lines = ["Position your phone camera,", "start recording, then", "press ENTER to begin", "(q / ESC to cancel)"]
    canvas = np.full((window_size, window_size), 255, dtype=np.uint8)
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = window_size / 800.0
    line_height = int(50 * scale)
    total_h = line_height * len(lines)
    y0 = (window_size - total_h) // 2
    for i, line in enumerate(lines):
        (tw, th), _ = cv2.getTextSize(line, font, scale, max(1, int(2 * scale)))
        org = ((window_size - tw) // 2, y0 + i * line_height + th)
        cv2.putText(canvas, line, org, font, scale, (0,), max(1, int(2 * scale)), cv2.LINE_AA)
    cv2.imshow(window_name, canvas)
    while True:
        key = cv2.waitKey(50) & 0xFF
        if key in (13, 10):  # ENTER (CR or LF depending on platform)
            return True
        if key in (ord("q"), 27):  # q or ESC
            return False


def cmd_encode(args: argparse.Namespace) -> int:
    input_path = Path(args.input_file)
    if not input_path.is_file():
        print(f"error: input file not found: {input_path}", file=sys.stderr)
        return 1

    data = input_path.read_bytes()
    file_hash = hashlib.sha256(data).hexdigest()
    chunk_size = args.chunk_size

    if len(data) == 0:
        chunks = [b""]
    else:
        chunks = [data[i:i + chunk_size] for i in range(0, len(data), chunk_size)]
    total_chunks = len(chunks)
    total_frames = total_chunks + 1  # + metadata frame

    metadata = {
        "filename": input_path.name,
        "filesize": len(data),
        "chunk_size": chunk_size,
        "total_chunks": total_chunks,
        "sha256": file_hash,
    }
    meta_payload = json.dumps(metadata).encode("utf-8")

    frames = [pack_frame(0, total_frames, FRAME_TYPE_META, meta_payload)]
    for i, chunk in enumerate(chunks, start=1):
        frames.append(pack_frame(i, total_frames, FRAME_TYPE_DATA, chunk))

    fps = 1000.0 / args.duration_ms
    video_seconds = total_frames * args.duration_ms / 1000.0

    print(f"File: {input_path.name} ({len(data)} bytes)")
    print(f"SHA256: {file_hash}")
    print(f"Chunks: {total_chunks} (chunk_size={chunk_size}) -> {total_frames} frames total")
    print(f"Displaying at {args.duration_ms} ms/frame ({fps:.1f} fps)")
    print(f"Expected recording length: ~{video_seconds:.1f}s "
          f"({int(video_seconds // 60)}m{video_seconds % 60:04.1f}s)")

    if args.info:
        return 0

    window_name = "QR Transfer - Encode"
    cv2.namedWindow(window_name, cv2.WINDOW_AUTOSIZE)

    try:
        if not args.no_wait:
            print("Window ready. Position your phone camera, start recording, then press ENTER in the window to begin (q/ESC to cancel).")
            if not wait_for_enter_screen(args.window_size, window_name):
                print("Aborted by user.")
                return 1

        if args.countdown > 0:
            print(f"Starting in {args.countdown} seconds...")
            countdown_screen(args.window_size, args.countdown, window_name)

        start = time.time()
        for idx, frame_bytes in enumerate(frames):
            img = make_qr_image(frame_bytes, args.window_size)
            cv2.imshow(window_name, img)
            key = cv2.waitKey(args.duration_ms) & 0xFF
            if key in (ord("q"), 27):  # q or ESC
                print("Aborted by user.")
                return 1
            if (idx + 1) % 25 == 0 or (idx + 1) == len(frames):
                print(f"  displayed {idx + 1}/{len(frames)} frames", end="\r", flush=True)

        elapsed = time.time() - start
        print()
        print(f"Done. Displayed {len(frames)} frames in {elapsed:.1f}s.")
        return 0
    except KeyboardInterrupt:
        print()
        print("Interrupted (Ctrl+C). Aborting cleanly.")
        return 130
    finally:
        cv2.destroyAllWindows()


# --------------------------------------------------------------------------
# Decode
# --------------------------------------------------------------------------

def decode_qr_opencv(qr_detector, gray) -> list[str]:
    try:
        retval, decoded_infos, _points, _straight = qr_detector.detectAndDecodeMulti(gray)
    except cv2.error:
        retval, decoded_infos = False, []
    if not retval:
        single, _points, _straight = qr_detector.detectAndDecode(gray)
        decoded_infos = [single] if single else []
    return [text for text in decoded_infos if text]


def decode_qr_pyzbar(zbar_decode, ZBarSymbol, gray) -> list[str]:
    results = zbar_decode(gray, symbols=[ZBarSymbol.QRCODE])
    texts = []
    for result in results:
        try:
            texts.append(result.data.decode("ascii"))
        except UnicodeDecodeError:
            continue  # not one of our (base64, ASCII-only) frames
    return texts


def decode_qr_zxing(zxingcpp, gray) -> list[str]:
    results = zxingcpp.read_barcodes(gray, formats=zxingcpp.BarcodeFormat.QRCode)
    return [r.text for r in results if r.text]


DECODER_NAMES = ("opencv", "pyzbar", "zxing")


class DecoderSetupError(Exception):
    pass


def build_decoders(names: list[str]):
    """Lazily set up the requested decoding backend(s).

    Only imports third-party QR libraries (pyzbar, zxing-cpp) if they were
    actually requested via --decoder, so the default 'opencv' path never
    touches them. Returns a list of (name, decode_fn) pairs where decode_fn
    takes a grayscale image and returns a list of decoded text strings.
    """
    decoders = []
    for name in names:
        if name == "opencv":
            qr_detector = cv2.QRCodeDetector()
            decoders.append(("opencv", lambda gray, d=qr_detector: decode_qr_opencv(d, gray)))
        elif name == "pyzbar":
            try:
                from pyzbar.pyzbar import decode as zbar_decode, ZBarSymbol
            except ImportError:
                raise DecoderSetupError(
                    "--decoder pyzbar requires the 'pyzbar' package (and its native zbar "
                    "library). Install it with:\n  pip install pyzbar")
            decoders.append(("pyzbar", lambda gray, zd=zbar_decode, zs=ZBarSymbol: decode_qr_pyzbar(zd, zs, gray)))
        elif name == "zxing":
            try:
                import zxingcpp
            except ImportError:
                raise DecoderSetupError(
                    "--decoder zxing requires the 'zxing-cpp' package. Install it with:\n"
                    "  pip install zxing-cpp")
            decoders.append(("zxing", lambda gray, zx=zxingcpp: decode_qr_zxing(zx, gray)))
        else:
            raise DecoderSetupError(
                f"unknown decoder '{name}', expected one of: {', '.join(DECODER_NAMES)}")
    return decoders


def cmd_decode(args: argparse.Namespace) -> int:
    requested = [n.strip().lower() for n in args.decoder.split(",") if n.strip()]
    if not requested:
        print("error: --decoder requires at least one backend name", file=sys.stderr)
        return 1
    try:
        decoders = build_decoders(requested)
    except DecoderSetupError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    video_path = Path(args.video_file)
    if not video_path.is_file():
        print(f"error: video file not found: {video_path}", file=sys.stderr)
        return 1

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        print(f"error: could not open video: {video_path}", file=sys.stderr)
        print("This usually means the container/codec isn't supported by the "
              "FFmpeg build bundled with opencv-python. Try re-encoding it to a "
              "plain H.264 MP4 first, e.g.:", file=sys.stderr)
        print(f"  ffmpeg -i \"{video_path}\" -c:v libx264 -crf 18 \"{video_path.stem}_h264.mp4\"",
              file=sys.stderr)
        print("then run decode again on the converted file.", file=sys.stderr)
        return 1

    metadata = None
    chunks: dict[int, bytes] = {}
    frame_num = 0
    total_video_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or None

    print(f"Scanning {video_path.name} (decoder={','.join(name for name, _ in decoders)}) ...")
    interrupted = False
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            frame_num += 1

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            texts: list[str] = []
            for _name, decode_fn in decoders:
                texts.extend(decode_fn(gray))
            for text in texts:
                try:
                    raw = base64.b64decode(text, validate=True)
                except (base64.binascii.Error, ValueError):
                    continue  # not one of our frames
                parsed = unpack_frame(raw)
                if parsed is None:
                    continue  # not one of our frames, or corrupted beyond CRC check

                if parsed["frame_type"] == FRAME_TYPE_META:
                    if metadata is None:
                        try:
                            metadata = json.loads(parsed["payload"].decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            continue
                        print(f"  found metadata: {metadata['filename']} "
                              f"({metadata['filesize']} bytes, {metadata['total_chunks']} chunks)")
                    continue

                idx = parsed["frame_index"]
                if idx not in chunks:
                    chunks[idx] = parsed["payload"]
                    found = len(chunks)
                    total = metadata["total_chunks"] if metadata else "?"
                    print(f"  chunk {idx} captured ({found}/{total})", end="\r", flush=True)

            if total_video_frames:
                if frame_num % 30 == 0:
                    print(f"  processed frame {frame_num}/{total_video_frames}", end="\r", flush=True)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        cap.release()
    print()

    if interrupted:
        found = len(chunks)
        total = metadata["total_chunks"] if metadata else "?"
        print(f"Interrupted (Ctrl+C) after scanning {frame_num} frames "
              f"({found}/{total} chunks captured so far). Aborting without writing output.",
              file=sys.stderr)
        return 130

    if metadata is None:
        print("error: metadata frame was never captured. Re-record the video (make sure "
              "the very first frame is captured too).", file=sys.stderr)
        return 1

    total_chunks = metadata["total_chunks"]
    missing = [i for i in range(1, total_chunks + 1) if i not in chunks]
    if missing:
        print(f"error: {len(missing)}/{total_chunks} chunks missing after scanning the video.",
              file=sys.stderr)
        print(f"missing chunk indices: {missing}", file=sys.stderr)
        print("Re-record the video (e.g. slower playback, closer/steadier camera, "
              "better lighting) and try again.", file=sys.stderr)
        return 1

    data = b"".join(chunks[i] for i in range(1, total_chunks + 1))
    actual_hash = hashlib.sha256(data).hexdigest()
    if actual_hash != metadata["sha256"]:
        print("error: reconstructed file hash does not match expected sha256.", file=sys.stderr)
        print(f"  expected: {metadata['sha256']}", file=sys.stderr)
        print(f"  actual:   {actual_hash}", file=sys.stderr)
        return 1

    if args.output:
        output_path = Path(args.output)
    else:
        output_path = Path(metadata["filename"])

    if output_path.exists() and not args.force:
        print(f"error: output file already exists: {output_path} (use --force to overwrite)",
              file=sys.stderr)
        return 1

    output_path.write_bytes(data)
    print(f"OK: wrote {len(data)} bytes to {output_path} (sha256 verified).")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_encode = sub.add_parser("encode", help="Encode a file into a sequence of QR codes shown on screen.")
    p_encode.add_argument("input_file", help="Path to the file to transmit.")
    p_encode.add_argument("--duration-ms", type=int, default=200,
                           help="Milliseconds each QR frame stays on screen (default: 200).")
    p_encode.add_argument("--chunk-size", type=int, default=1000,
                           help="Bytes of file data per QR frame (default: 1000).")
    p_encode.add_argument("--window-size", type=int, default=800,
                           help="Width/height in pixels of the display window (default: 800).")
    p_encode.add_argument("--countdown", type=int, default=5,
                           help="Seconds of countdown shown before the first QR frame (default: 5, 0 to disable).")
    p_encode.add_argument("--no-wait", action="store_true",
                           help="Skip the 'press ENTER to begin' screen and go straight to the countdown/frames.")
    p_encode.add_argument("--info", action="store_true",
                           help="Print chunk count and expected recording length, then exit "
                                "without opening a display window.")
    p_encode.set_defaults(func=cmd_encode)

    p_decode = sub.add_parser("decode", help="Decode a screen recording video back into the original file.")
    p_decode.add_argument("video_file", help="Path to the recorded video file.")
    p_decode.add_argument("-o", "--output", help="Output file path (default: embedded original filename).")
    p_decode.add_argument("--force", action="store_true", help="Overwrite output file if it already exists.")
    p_decode.add_argument("--decoder", default="opencv",
                           help="QR decoding backend(s) to use, comma-separated: opencv, pyzbar, "
                                "zxing (default: opencv). Each requested backend is lazily "
                                "imported only when selected, so e.g. the default 'opencv' never "
                                "touches pyzbar/zxing-cpp. Multiple backends can be combined, e.g. "
                                "--decoder pyzbar,zxing, running all of them on every frame and "
                                "merging the results - useful when one backend misses chunks the "
                                "other catches. 'pyzbar' needs 'pip install pyzbar' (plus its "
                                "native zbar library, which can be unavailable in some "
                                "locked-down/VDI environments). 'zxing' needs "
                                "'pip install zxing-cpp' (self-contained wheel, no external "
                                "native library to locate).")
    p_decode.set_defaults(func=cmd_decode)

    args = parser.parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print()
        print("Interrupted (Ctrl+C).", file=sys.stderr)
        cv2.destroyAllWindows()
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
