"""Install public pretrained speaker models into the local model cache."""
import hashlib
from pathlib import Path
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / ".cache" / "shared-perception"
ASSETS = {
    "speaker.onnx": "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx",
    "segmentation.tar.bz2": "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2",
}
SHA256 = {
    "speaker.onnx": "f682b514c05d947ee3fa91cd6ec6c5c7543479a128373fa29b1faedccd21fd11",
    "segmentation.tar.bz2": "24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488",
}


def main():
    DEST.mkdir(parents=True, exist_ok=True)
    for name, url in ASSETS.items():
        target = DEST / name
        if not target.exists():
            temporary = target.with_suffix(target.suffix + ".download")
            urllib.request.urlretrieve(url, temporary)
            if hashlib.sha256(temporary.read_bytes()).hexdigest() != SHA256[name]:
                raise ValueError("Downloaded model checksum mismatch: " + name)
            temporary.replace(target)
        if hashlib.sha256(target.read_bytes()).hexdigest() != SHA256[name]:
            raise ValueError("Cached model checksum mismatch: " + name)
        print(name, target.stat().st_size, hashlib.sha256(target.read_bytes()).hexdigest(), flush=True)
    with tarfile.open(DEST / "segmentation.tar.bz2") as archive:
        member = next(m for m in archive.getmembers() if m.isfile() and m.name.endswith("model.onnx"))
        with archive.extractfile(member) as source:
            (DEST / "segmentation.onnx").write_bytes(source.read())


if __name__ == "__main__":
    main()
