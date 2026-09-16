/**
 * ACCLLMS - In-Portal Attendance Camera QR Scanner
 * Built with Html5Qrcode for cross-device mobile & tablet camera access.
 * Includes Android & iOS camera permission guidance + native camera photo capture fallback.
 */

let html5QrCode = null;
let currentCameraId = null;
let camerasList = [];
let isScanning = false;

function openAttendanceScanner() {
  const modal = document.getElementById("qrScannerModal");
  if (!modal) return;

  modal.style.display = "flex";
  document.body.style.overflow = "hidden"; // prevent background scrolling

  // Reset status and guide
  const guideEl = document.getElementById("scannerPermGuide");
  if (guideEl) guideEl.style.display = "none";

  const httpNotice = document.getElementById("scannerHttpNotice");
  if (httpNotice) httpNotice.style.display = "none";

  const readerDiv = document.getElementById("qrReader");
  if (readerDiv) readerDiv.style.display = "block";

  const laser = document.querySelector(".scanner-laser-line");
  if (laser) laser.style.display = "block";

  const statusEl = document.getElementById("scannerStatus");
  if (statusEl) {
    statusEl.innerHTML = '<span class="pulse-dot" style="width:8px;height:8px;"></span> Starting camera...';
    statusEl.style.color = "var(--text-muted)";
  }

  // Detect insecure HTTP context only when getUserMedia is truly unavailable in standard browser
  const isAndroidApp = !!window.Android;
  const hasGetUserMedia = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  const isSecure = window.isSecureContext || window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1";

  if (!isAndroidApp && !hasGetUserMedia && !isSecure && window.location.protocol === "http:") {
    showCameraInsecureHttpNotice();
    return;
  }

  setTimeout(initCameraScanner, 250);
}

function closeAttendanceScanner() {
  const modal = document.getElementById("qrScannerModal");
  if (modal) {
    modal.style.display = "none";
  }
  document.body.style.overflow = "";
  stopCameraScanner();

  const readerDiv = document.getElementById("qrReader");
  if (readerDiv) readerDiv.style.display = "block";

  const laser = document.querySelector(".scanner-laser-line");
  if (laser) laser.style.display = "block";

  const guideEl = document.getElementById("scannerPermGuide");
  if (guideEl) guideEl.style.display = "none";

  const httpNotice = document.getElementById("scannerHttpNotice");
  if (httpNotice) httpNotice.style.display = "none";
}

let currentZoom = 1.0;
let activeVideoTrack = null;
let touchStartDistance = 0;
let touchStartZoom = 1.0;

function setupCameraZoom() {
  currentZoom = 1.0;
  activeVideoTrack = null;
  const videoEl = document.querySelector("#qrReader video");
  if (videoEl && videoEl.srcObject) {
    const tracks = videoEl.srcObject.getVideoTracks();
    if (tracks && tracks.length > 0) {
      activeVideoTrack = tracks[0];
    }
  }

  const zoomBar = document.getElementById("scannerZoomBar");
  if (zoomBar) zoomBar.style.display = "flex";
  updateZoomUI(1.0);

  // Setup pinch-to-zoom on the reader container
  const readerDiv = document.getElementById("qrReader");
  if (readerDiv && !readerDiv._pinchAttached) {
    readerDiv._pinchAttached = true;
    readerDiv.addEventListener("touchstart", (e) => {
      if (e.touches.length === 2) {
        touchStartDistance = Math.hypot(
          e.touches[0].clientX - e.touches[1].clientX,
          e.touches[0].clientY - e.touches[1].clientY
        );
        touchStartZoom = currentZoom;
      }
    }, { passive: true });

    readerDiv.addEventListener("touchmove", (e) => {
      if (e.touches.length === 2 && touchStartDistance > 0) {
        const dist = Math.hypot(
          e.touches[0].clientX - e.touches[1].clientX,
          e.touches[0].clientY - e.touches[1].clientY
        );
        const factor = dist / touchStartDistance;
        const newZoom = Math.min(Math.max(touchStartZoom * factor, 1.0), 4.0);
        setScannerZoom(newZoom);
      }
    }, { passive: true });

    readerDiv.addEventListener("touchend", (e) => {
      if (e.touches.length < 2) {
        touchStartDistance = 0;
      }
    }, { passive: true });
  }
}

function setScannerZoom(level) {
  const zoom = Math.min(Math.max(parseFloat(level) || 1.0, 1.0), 4.0);
  currentZoom = zoom;

  // 1. Hardware Optical Zoom if supported by browser/camera track
  if (activeVideoTrack && typeof activeVideoTrack.getCapabilities === "function") {
    try {
      const caps = activeVideoTrack.getCapabilities();
      if (caps && caps.zoom) {
        const minZ = caps.zoom.min || 1.0;
        const maxZ = caps.zoom.max || 1.0;
        const hwZoom = Math.min(Math.max(zoom, minZ), maxZ);
        activeVideoTrack.applyConstraints({
          advanced: [{ zoom: hwZoom }]
        }).catch(() => {});
      }
    } catch (e) {}
  }

  // 2. Digital CSS Zoom on video preview element (works universally)
  const videoEl = document.querySelector("#qrReader video");
  if (videoEl) {
    videoEl.style.transformOrigin = "center center";
    videoEl.style.transform = `scale(${zoom})`;
    videoEl.style.transition = "transform 0.15s ease";
  }

  updateZoomUI(zoom);
}

function adjustScannerZoom(delta) {
  setScannerZoom(currentZoom + delta);
}

function updateZoomUI(zoom) {
  const rounded = Math.round(zoom);
  for (let i = 1; i <= 4; i++) {
    const btn = document.getElementById("zoomBtn" + i);
    if (btn) {
      if (rounded === i) {
        btn.classList.add("active");
      } else {
        btn.classList.remove("active");
      }
    }
  }
}

function stopCameraScanner() {
  const zoomBar = document.getElementById("scannerZoomBar");
  if (zoomBar) zoomBar.style.display = "none";
  activeVideoTrack = null;
  currentZoom = 1.0;
  const videoEl = document.querySelector("#qrReader video");
  if (videoEl) {
    videoEl.style.transform = "none";
  }

  if (html5QrCode && isScanning) {
    html5QrCode.stop().then(() => {
      isScanning = false;
      try { html5QrCode.clear(); } catch (e) {}
    }).catch(err => {
      console.warn("Error stopping scanner:", err);
      isScanning = false;
    });
  }
}

function initCameraScanner() {
  const readerDiv = document.getElementById("qrReader");
  if (!readerDiv) return;

  if (typeof Html5Qrcode === "undefined") {
    showCameraPermissionGuide("Scanner library not loaded. Please refresh the page.");
    return;
  }

  if (!html5QrCode) {
    html5QrCode = new Html5Qrcode("qrReader");
  }

  const config = {
    fps: 15,
    qrbox: function(viewfinderWidth, viewfinderHeight) {
      const minEdge = Math.min(viewfinderWidth, viewfinderHeight);
      const edgeSize = Math.floor(minEdge * 0.75);
      return { width: edgeSize, height: edgeSize };
    },
    aspectRatio: 1.0,
    videoConstraints: {
      facingMode: "environment"
    }
  };

  Html5Qrcode.getCameras().then(devices => {
    camerasList = devices || [];
    const switchBtn = document.getElementById("switchCameraBtn");
    if (switchBtn) {
      switchBtn.style.display = camerasList.length > 1 ? "inline-flex" : "none";
    }

    const backCamera = camerasList.find(c => 
      c.label.toLowerCase().includes("back") || 
      c.label.toLowerCase().includes("rear") || 
      c.label.toLowerCase().includes("environment")
    );
    const cameraId = backCamera ? backCamera.id : (camerasList[0] ? camerasList[0].id : null);
    currentCameraId = cameraId;

    const cameraParam = cameraId ? { deviceId: { exact: cameraId } } : { facingMode: "environment" };

    html5QrCode.start(
      cameraParam,
      config,
      onQrCodeSuccess,
      onQrCodeProgress
    ).then(() => {
      isScanning = true;
      setTimeout(setupCameraZoom, 400);
      const statusEl = document.getElementById("scannerStatus");
      if (statusEl) {
        statusEl.innerHTML = '🟢 <strong>Camera active:</strong> Point at the projector screen QR code';
      }
    }).catch(err => {
      console.warn("Camera start error with deviceId, trying facingMode:", err);
      html5QrCode.start(
        { facingMode: "environment" },
        config,
        onQrCodeSuccess,
        onQrCodeProgress
      ).then(() => {
        isScanning = true;
        setTimeout(setupCameraZoom, 400);
      }).catch(fallbackErr => {
        console.error("Camera permission denied or unavailable:", fallbackErr);
        showCameraPermissionGuide("Camera permission was not granted by your mobile browser.");
      });
    });
  }).catch(err => {
    console.warn("Could not list cameras, trying facingMode environment:", err);
    html5QrCode.start(
      { facingMode: "environment" },
      config,
      onQrCodeSuccess,
      onQrCodeProgress
    ).then(() => {
      isScanning = true;
      setTimeout(setupCameraZoom, 400);
    }).catch(permErr => {
      console.error("Camera permission blocked:", permErr);
      showCameraPermissionGuide("Camera permission was blocked or is not supported over HTTP.");
    });
  });
}

function switchCamera() {
  if (!html5QrCode || camerasList.length <= 1) return;
  stopCameraScanner();

  const currentIndex = camerasList.findIndex(c => c.id === currentCameraId);
  const nextIndex = (currentIndex + 1) % camerasList.length;
  currentCameraId = camerasList[nextIndex].id;

  setTimeout(initCameraScanner, 300);
}

function onQrCodeSuccess(decodedText, decodedResult) {
  if (!decodedText) return;

  if (navigator.vibrate) {
    try { navigator.vibrate([80, 40, 80]); } catch (e) {}
  }

  const statusEl = document.getElementById("scannerStatus");
  if (statusEl) {
    statusEl.innerHTML = '🎉 <strong>QR Code Recognized!</strong> Redirecting...';
    statusEl.style.color = "#16a34a";
  }

  stopCameraScanner();

  const prefix = window.location.pathname.startsWith("/lms") ? "/lms" : (window.location.pathname.startsWith("/hoodle") ? "/hoodle" : "");

  if (decodedText.startsWith("http://") || decodedText.startsWith("https://")) {
    window.location.href = decodedText;
  } else if (decodedText.startsWith("/")) {
    let target = decodedText;
    if (prefix && !target.startsWith(prefix)) {
      target = prefix + target;
    }
    window.location.href = target;
  } else if (decodedText.includes("attend/")) {
    const cleanPath = decodedText.replace(/^\/+/, "");
    window.location.href = (prefix ? prefix + "/" : "/") + cleanPath;
  } else {
    alert("Scanned text: " + decodedText + "\nPlease scan the official ACCLLMS projector code.");
  }
}

function onQrCodeProgress(errorMessage) {
  // scanning frame
}

// Show insecure HTTP notice when live video is disabled by browser security
function showCameraInsecureHttpNotice() {
  const laser = document.querySelector(".scanner-laser-line");
  if (laser) laser.style.display = "none";

  const readerDiv = document.getElementById("qrReader");
  if (readerDiv) readerDiv.style.display = "none";

  const permGuide = document.getElementById("scannerPermGuide");
  if (permGuide) permGuide.style.display = "none";

  const httpNotice = document.getElementById("scannerHttpNotice");
  if (httpNotice) httpNotice.style.display = "block";

  const statusEl = document.getElementById("scannerStatus");
  if (statusEl) {
    statusEl.innerHTML = `<span style="color:#b45309; font-weight:700;">🔒 Secure HTTPS Required for Live Camera</span>`;
  }
}

// Display step-by-step guidance for Android and iOS when camera permission fails
function showCameraPermissionGuide(errorMsg) {
  const isSecure = window.isSecureContext || window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1";
  if (!isSecure && window.location.protocol === "http:") {
    showCameraInsecureHttpNotice();
    return;
  }

  const laser = document.querySelector(".scanner-laser-line");
  if (laser) laser.style.display = "none";

  const readerDiv = document.getElementById("qrReader");
  if (readerDiv) readerDiv.style.display = "none";

  const httpNotice = document.getElementById("scannerHttpNotice");
  if (httpNotice) httpNotice.style.display = "none";

  const guideEl = document.getElementById("scannerPermGuide");
  if (guideEl) guideEl.style.display = "block";

  const statusEl = document.getElementById("scannerStatus");
  if (statusEl) {
    statusEl.innerHTML = `<span style="color:#dc2626; font-weight:700;">⚠️ Camera Access Blocked</span>`;
  }
}

function switchPermTab(os) {
  const tabAndroid = document.getElementById("tabAndroid");
  const tabIOS = document.getElementById("tabIOS");
  const contentAndroid = document.getElementById("guideAndroid");
  const contentIOS = document.getElementById("guideIOS");

  if (os === "ios") {
    if (tabIOS) tabIOS.classList.add("active");
    if (tabAndroid) tabAndroid.classList.remove("active");
    if (contentIOS) contentIOS.style.display = "block";
    if (contentAndroid) contentAndroid.style.display = "none";
  } else {
    if (tabAndroid) tabAndroid.classList.add("active");
    if (tabIOS) tabIOS.classList.remove("active");
    if (contentAndroid) contentAndroid.style.display = "block";
    if (contentIOS) contentIOS.style.display = "none";
  }
}

// Fallback: Student takes photo with native Android/iOS camera app
function onQrPhotoSelected(input) {
  if (!input.files || input.files.length === 0) return;
  const file = input.files[0];

  const statusEl = document.getElementById("scannerStatus");
  if (statusEl) {
    statusEl.innerHTML = '<span class="pulse-dot" style="width:8px;height:8px;"></span> Analyzing captured photo...';
    statusEl.style.color = "var(--primary)";
  }

  // Ensure an element exists for Html5Qrcode to attach canvas
  let readerDiv = document.getElementById("qrReader");
  if (!readerDiv) {
    readerDiv = document.createElement("div");
    readerDiv.id = "qrReader";
    readerDiv.style.display = "none";
    document.body.appendChild(readerDiv);
  }

  if (!html5QrCode) {
    html5QrCode = new Html5Qrcode("qrReader");
  }

  html5QrCode.scanFile(file, true)
    .then(decodedText => {
      onQrCodeSuccess(decodedText);
    })
    .catch(err => {
      console.error("Error scanning photo:", err);
      if (statusEl) {
        statusEl.innerHTML = '<span style="color:#dc2626; font-weight:700;">❌ QR Code not detected in photo. Please point closer and try again.</span>';
      }
    });
}

// Close scanner with ESC key
document.addEventListener("keydown", function(e) {
  if (e.key === "Escape") {
    closeAttendanceScanner();
  }
});
