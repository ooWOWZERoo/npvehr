// New Path Vision EHR -- Voice Scribe: click-to-toggle in-browser dictation
// into a specific free-text field, using a small WASM Whisper model
// (@huggingface/transformers, vendored locally under
// /static/js/vendor/transformers/ -- MIT, no build step, matches this app's
// existing FullCalendar vendoring convention). Audio is captured and
// transcribed entirely in the browser; recorded speech is never sent
// anywhere. The transformers.js *library* is vendored locally; the Whisper
// model weights and the ONNX Runtime Web WASM backend are both large,
// generic (non-patient) binary assets fetched once per browser from their
// public CDNs and cached thereafter (transformers.js's own IndexedDB/Cache
// API caching) -- a fundamentally different trust boundary than sending
// recorded dictation audio anywhere, so this doesn't conflict with the
// "audio never leaves the machine" goal this feature exists for.
(function () {
  "use strict";

  var MODEL_ID = "Xenova/whisper-small.en";
  var TRANSFORMERS_URL = "/static/js/vendor/transformers/transformers.web.min.js";
  // The vendored onnxruntime-web webgpu bundle
  // (vendor/onnxruntime-web/ort.webgpu.bundle.min.1.30.0.mjs) resolves its
  // own .wasm binary relative to its own module URL by default, which
  // would mean serving a ~14-28MB binary from this app's own static files.
  // That binary is the same kind of large, generic (non-patient) one-time
  // download as the Whisper model weights themselves, so point it at the
  // matching published onnxruntime-web version on jsdelivr instead --
  // must match ONNXRUNTIME_WEB_VERSION exactly, since the .wasm binary's
  // shape is version-specific. Keep in sync with the README in that
  // vendored directory when upgrading.
  var ONNXRUNTIME_WEB_VERSION = "1.30.0";
  var ONNXRUNTIME_WEB_WASM_BASE_URL =
    "https://cdn.jsdelivr.net/npm/onnxruntime-web@" + ONNXRUNTIME_WEB_VERSION + "/dist/";

  var transformersModulePromise = null;
  var pipelinePromise = null;

  function configureWasmPaths(mod) {
    var onnx = mod && mod.env && mod.env.backends && mod.env.backends.onnx;
    if (onnx && onnx.wasm) {
      onnx.wasm.wasmPaths = ONNXRUNTIME_WEB_WASM_BASE_URL;
    }
  }

  function loadTransformers() {
    if (!transformersModulePromise) {
      transformersModulePromise = import(TRANSFORMERS_URL).then(function (mod) {
        configureWasmPaths(mod);
        return mod;
      });
    }
    return transformersModulePromise;
  }

  function getPipeline(onProgress) {
    if (!pipelinePromise) {
      pipelinePromise = loadTransformers().then(function (mod) {
        return mod.pipeline("automatic-speech-recognition", MODEL_ID, {
          progress_callback: onProgress,
        });
      });
    }
    return pipelinePromise;
  }

  function findField(targetId) {
    return document.getElementById(targetId) || document.querySelector('[name="' + targetId + '"]');
  }

  function setState(btn, state) {
    btn.classList.remove("vs-idle", "vs-loading", "vs-recording", "vs-transcribing", "vs-error");
    btn.classList.add("vs-" + state);
    btn.disabled = (state === "loading" || state === "transcribing");
  }

  function fireChangeEvents(field) {
    // Setting .value directly does not fire 'input'/'change' -- this
    // codebase's own live composers (exams/form.html's refresh()) listen
    // for real events on tracked fields, so a dictated value must trigger
    // them the same way a real keystroke would (see the follow_up_unit /
    // exam_type_confirmed edited-flag bugs this exact codebase already hit
    // for the same underlying reason).
    field.dispatchEvent(new Event("input", { bubbles: true }));
    field.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function insertText(field, text) {
    text = (text || "").trim();
    if (!text) return;
    var existing = field.value || "";
    if (existing && !/\s$/.test(existing)) {
      existing += " ";
    }
    field.value = existing + text;
    fireChangeEvents(field);
  }

  function mixToMono(audioBuffer) {
    var length = audioBuffer.length;
    var out = new Float32Array(length);
    for (var ch = 0; ch < audioBuffer.numberOfChannels; ch++) {
      var data = audioBuffer.getChannelData(ch);
      for (var i = 0; i < length; i++) out[i] += data[i] / audioBuffer.numberOfChannels;
    }
    return out;
  }

  function decodeToFloat32(blob) {
    return blob.arrayBuffer().then(function (arrayBuffer) {
      var AudioCtx = window.AudioContext || window.webkitAudioContext;
      var audioCtx = new AudioCtx({ sampleRate: 16000 });
      return audioCtx.decodeAudioData(arrayBuffer).then(function (audioBuffer) {
        var channelData = audioBuffer.numberOfChannels > 1 ? mixToMono(audioBuffer) : audioBuffer.getChannelData(0);
        audioCtx.close();
        return channelData;
      });
    });
  }

  function initButton(btn) {
    var targetId = btn.getAttribute("data-target");
    var field = findField(targetId);
    if (!field) return;

    var mediaRecorder = null;
    var chunks = [];
    var recording = false;

    setState(btn, "idle");
    btn.setAttribute("aria-pressed", "false");

    btn.addEventListener("click", function () {
      if (btn.disabled) return;
      if (!recording) {
        startRecording();
      } else {
        stopRecording();
      }
    });

    function startRecording() {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || typeof MediaRecorder === "undefined") {
        setState(btn, "error");
        btn.title = "Voice dictation is not supported in this browser.";
        return;
      }
      navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
        chunks = [];
        mediaRecorder = new MediaRecorder(stream);
        mediaRecorder.ondataavailable = function (e) {
          if (e.data && e.data.size > 0) chunks.push(e.data);
        };
        mediaRecorder.onstop = function () {
          stream.getTracks().forEach(function (t) { t.stop(); });
          handleRecordingStopped();
        };
        mediaRecorder.start();
        recording = true;
        setState(btn, "recording");
        btn.setAttribute("aria-pressed", "true");
        btn.title = "Click to stop dictating";
      }).catch(function () {
        setState(btn, "error");
        btn.title = "Microphone access was denied or unavailable.";
      });
    }

    function stopRecording() {
      recording = false;
      btn.setAttribute("aria-pressed", "false");
      if (mediaRecorder && mediaRecorder.state !== "inactive") {
        mediaRecorder.stop();
      }
    }

    function handleRecordingStopped() {
      var blob = new Blob(chunks, { type: "audio/webm" });
      var firstLoad = !pipelinePromise;
      setState(btn, firstLoad ? "loading" : "transcribing");
      btn.title = firstLoad ? "Loading voice model (first use only)..." : "Transcribing...";

      getPipeline(function (progress) {
        if (progress && progress.status === "progress" && typeof progress.progress === "number") {
          btn.title = "Loading voice model... " + Math.round(progress.progress) + "%";
        }
      }).then(function (transcriber) {
        setState(btn, "transcribing");
        btn.title = "Transcribing...";
        return decodeToFloat32(blob).then(function (audioData) {
          return transcriber(audioData);
        });
      }).then(function (result) {
        var text = Array.isArray(result) ? (result[0] && result[0].text) : (result && result.text);
        insertText(field, text);
        setState(btn, "idle");
        btn.title = "Voice dictation";
      }).catch(function (err) {
        console.error("Voice scribe transcription failed:", err);
        setState(btn, "error");
        btn.title = "Transcription failed -- click to try again.";
        window.setTimeout(function () {
          setState(btn, "idle");
          btn.title = "Voice dictation";
        }, 3000);
      });
    }
  }

  function init() {
    var buttons = document.querySelectorAll(".voice-scribe-btn[data-target]");
    for (var i = 0; i < buttons.length; i++) initButton(buttons[i]);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
