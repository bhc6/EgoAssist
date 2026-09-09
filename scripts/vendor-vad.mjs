import {copyFile, mkdir} from "node:fs/promises";
import {resolve} from "node:path";

const destination = resolve("egoassist/console/vendor/vad");
await mkdir(destination, {recursive: true});
for (const file of ["bundle.min.js", "vad.worklet.bundle.min.js", "silero_vad_v5.onnx", "bundle.min.js.LICENSE.txt"]) {
  await copyFile(resolve("node_modules/@ricky0123/vad-web/dist", file), resolve(destination, file));
}
for (const file of ["ort.wasm.min.js", "ort-wasm-simd-threaded.mjs", "ort-wasm-simd-threaded.wasm"]) {
  await copyFile(resolve("node_modules/onnxruntime-web/dist", file), resolve(destination, file));
}
