"""Check real CUDA kernels and matching torchvision; fails without a GPU."""
import argparse
import json
import time
from pathlib import Path


def verify():
    report = {'passed': False}
    try:
        import torch
        import torchvision
        report.update(torch=torch.__version__, torchvision=torchvision.__version__,
                      cudaRuntime=torch.version.cuda, cudaAvailable=torch.cuda.is_available())
        if not report['cudaAvailable']:
            raise RuntimeError('CUDA device is unavailable')
        report.update(device=torch.cuda.get_device_name(0),
                      capability=torch.cuda.get_device_capability(0),
                      compiledArchitectures=torch.cuda.get_arch_list())
        torch.manual_seed(17)
        a, b = torch.randn(256, 256), torch.randn(256, 256)
        ag, bg = a.cuda(), b.cuda()
        ag @ bg  # warm up the actual kernel
        torch.cuda.synchronize()
        started = time.perf_counter()
        result = ag @ bg
        torch.cuda.synchronize()
        report['matmulMs'] = round((time.perf_counter() - started) * 1000, 3)
        torch.testing.assert_close(result.cpu(), a @ b, rtol=1e-3, atol=1e-3)
        boxes = torch.tensor([[0., 0., 10., 10.], [1., 1., 9., 9.]], device='cuda')
        kept = torchvision.ops.nms(boxes, torch.tensor([.9, .8], device='cuda'), .5)
        torch.cuda.synchronize()
        assert kept.tolist() == [0]
        report.update(passed=True, matmulVerified=True, torchvisionCudaNmsVerified=True)
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = verify()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result), flush=True)
    raise SystemExit(0 if result['passed'] else 1)
