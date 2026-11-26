export class H264View {
  constructor(canvas, img, onStatus) {
    this.canvas = canvas;
    this.img = img;
    this.onStatus = onStatus || (() => undefined);
    this.ws = null;
    this.decoder = null;
    this.description = null;
    this.configured = null;
    this.active = false;
    this.ctx = canvas.getContext("2d");
  }

  codecName(description) {
    if (!description || description.length < 8) return "avc1.640028";
    const len = (description[0] << 24) | (description[1] << 16) | (description[2] << 8) | description[3];
    const sps = description.subarray(4, 4 + len);
    const hex = (n) => Number(n || 0).toString(16).padStart(2, "0");
    return `avc1.${hex(sps[1])}${hex(sps[2])}${hex(sps[3])}`;
  }

  ensureDecoder() {
    if (!this.description || typeof VideoDecoder === "undefined") return false;
    if (!this.decoder || this.decoder.state === "closed") {
      this.decoder = new VideoDecoder({
        output: (frame) => {
          const w = frame.displayWidth || frame.codedWidth;
          const h = frame.displayHeight || frame.codedHeight;
          if (this.canvas.width !== w || this.canvas.height !== h) {
            this.canvas.width = w;
            this.canvas.height = h;
          }
          this.ctx.drawImage(frame, 0, 0, w, h);
          frame.close();
        },
        error: (error) => {
          this.onStatus(`解码失败：${error.message}`);
          this.stop();
        },
      });
    }
    if (this.decoder.state === "configured") return true;
    if (this.configured === this.description) return false;
    try {
      this.decoder.configure({
        codec: this.codecName(this.description),
        description: this.description,
        hardwareAcceleration: "prefer-hardware",
        optimizeForLatency: true,
      });
      this.configured = this.description;
      return true;
    } catch (error) {
      this.onStatus(`配置失败：${error.message}`);
      return false;
    }
  }

  start(url) {
    if (!url || typeof WebSocket === "undefined") return;
    if (this.ws && this.ws.url === url && this.ws.readyState <= WebSocket.OPEN) return;
    this.stop();
    this.active = true;
    this.img.classList.add("hidden");
    this.canvas.classList.remove("hidden");
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onmessage = (event) => {
      const data = new Uint8Array(event.data);
      if (!data.length) return;
      if (data[0] === 0) {
        this.description = data.slice(1);
        this.ensureDecoder();
      } else if (data[0] === 1 && this.ensureDecoder()) {
        if (this.decoder.decodeQueueSize > 2) return;
        try {
          this.decoder.decode(new EncodedVideoChunk({
            type: data[1] ? "key" : "delta",
            timestamp: Math.round(performance.now() * 1000),
            data: data.slice(2),
          }));
        } catch { /* 丢帧 */ }
      }
    };
    ws.onerror = () => this.onStatus("视频 WebSocket 异常");
  }

  stop() {
    this.active = false;
    if (this.ws) {
      try { this.ws.close(); } catch { /* ignore */ }
      this.ws = null;
    }
    if (this.decoder) {
      try { this.decoder.close(); } catch { /* ignore */ }
      this.decoder = null;
    }
    this.description = null;
    this.configured = null;
    this.canvas.classList.add("hidden");
    this.img.classList.remove("hidden");
  }

  showFallback(dataUrl) {
    if (this.active) return;
    if (dataUrl) this.img.src = dataUrl;
  }
}
