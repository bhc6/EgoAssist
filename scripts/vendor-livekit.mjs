import {copyFile, mkdir} from "node:fs/promises";
import {dirname, resolve} from "node:path";
import {fileURLToPath} from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const source = resolve(root, "node_modules/livekit-client/dist/livekit-client.umd.js");
const destination = resolve(root, "egoassist/console/vendor/livekit-client.umd.js");
await mkdir(dirname(destination), {recursive: true});
await copyFile(source, destination);
console.log(`Vendored LiveKit browser SDK to ${destination}`);

const pdfSource = resolve(root, "node_modules/pdfjs-dist/build/pdf.mjs");
const pdfWorkerSource = resolve(root, "node_modules/pdfjs-dist/build/pdf.worker.mjs");
const pdfDestination = resolve(root, "egoassist/console/vendor/pdf.mjs");
const pdfWorkerDestination = resolve(root, "egoassist/console/vendor/pdf.worker.mjs");
await copyFile(pdfSource, pdfDestination);
await copyFile(pdfWorkerSource, pdfWorkerDestination);
console.log(`Vendored PDF.js to ${pdfDestination}`);
