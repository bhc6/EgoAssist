/* Silero inference stays on the device; audio forwarding uses the existing transport. */
let localVad = null;
let localVadGeneration = 0;
async function startLocalVad(mediaStream) {
  const generation = ++localVadGeneration;
  if (localVad) { localVad.destroy(); localVad = null; }
  try {
    const instance = await vad.MicVAD.new({
      model: "v5", baseAssetPath: new URL("vendor/vad/", location.href).href,
      onnxWASMBasePath: new URL("vendor/vad/", location.href).href,
      getStream: async () => mediaStream, pauseStream: async () => {}, resumeStream: async () => mediaStream,
      ortConfig: runtime => { runtime.env.wasm.numThreads = 1; },
      onVADMisfire: () => {
        if (generation === localVadGeneration && socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({type: "vad_misfire"}));
      },
      onSpeechStart: () => {
        if (generation !== localVadGeneration) return;
        if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({type: "speech_activity"}));
        $("#duplexState").textContent = "正在听 · 根据语义决定是否接话";
      }
    });
    if (generation !== localVadGeneration) { instance.destroy(); return; }
    localVad = instance;
    document.documentElement.dataset.localVad = "ready";
  } catch (error) {
    if (generation !== localVadGeneration) return;
    document.documentElement.dataset.localVad = "unavailable";
    document.documentElement.dataset.localVadError = String(error);
    $("#duplexState").textContent = "本地语音检测不可用 · 使用云端语义交接";
    console.warn("Local VAD unavailable", error);
  }
}
function stopLocalVad() {
  localVadGeneration += 1;
  if (localVad) localVad.destroy();
  localVad = null;
}
