/**
 * ACCLLMS - In-Portal Attendance Camera QR Scanner
 * Built with Html5Qrcode for cross-device mobile & tablet camera access.
 */

let html5QrCode = null;
let currentCameraId = null;
let camerasList = [];
let isScanning = false;

function openAttendanceScanner() {
  const modal = document.getElementById("qrScannerModal");
  if (!modal) return;

  modal.style.display = "flex";
  document.body.style.overflow = "hidden"; // prevent scroll while scanning

  const statusEl = document.getElementById("scannerStatus");
  if (statusEl) {
    statusEl.innerHTML = '<span class="pulse-dot" style="width:8px;height:8px;"></span> Starting camera...';
  }

  // Delay slightly for modal layout rendering
  setTimeout(initCameraScanner, 250);
}

function closeAttendanceScanner() {
  const modal = document.getElementById("qrScannerModal");
  if (modal) {
    modal.style.display = "none";
  }
  document.body.style.overflow = "";

  stopCameraScanner();
}

function stopCameraScanner() {
  if (html5QrCode && isScanning) {
    html5QrCode.stop().then(() => {
      isScanning = false;
      try {
        html5QrCode.clear();
      } catch (e) {}
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
    showScannerError("Scanner library not loaded. Please refresh the page.");
    return;
  }

  if (!html5QrCode) {
    html5QrCode = new Html5Qrcode("qrReader");
  }

  // Config options for high performance and responsiveness
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

    // Prefer back/rear camera
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
      const statusEl = document.getElementById("scannerStatus");
      if (statusEl) {
        statusEl.innerHTML = '🟢 <strong>Camera active:</strong> Point at the projector screen QR code';
      }
    }).catch(err => {
      console.error("Camera start error:", err);
      // Fallback to simple facingMode if deviceId failed
      html5QrCode.start(
        { facingMode: "environment" },
        config,
        onQrCodeSuccess,
        onQrCodeProgress
      ).then(() => {
        isScanning = true;
      }).catch(fallbackErr => {
        showScannerError("Camera permission denied or camera unavailable. Please grant camera permissions in your browser settings.");
      });
    });
  }).catch(err => {
    console.warn("Could not list cameras:", err);
    // Try directly with facingMode: environment
    html5QrCode.start(
      { facingMode: "environment" },
      config,
      onQrCodeSuccess,
      onQrCodeProgress
    ).then(() => {
      isScanning = true;
    }).catch(permErr => {
      showScannerError("Please enable camera permissions to scan attendance QR codes.");
    });
  });
}

function switchCamera() {
  if (!html5QrCode || camerasList.length <= 1) return;

  stopCameraScanner();

  // Find next camera index
  const currentIndex = camerasList.findIndex(c => c.id === currentCameraId);
  const nextIndex = (currentIndex + 1) % camerasList.length;
  currentCameraId = camerasList[nextIndex].id;

  setTimeout(initCameraScanner, 300);
}

function onQrCodeSuccess(decodedText, decodedResult) {
  if (!decodedText) return;

  // Haptic feedback if supported on mobile
  if (navigator.vibrate) {
    try { navigator.vibrate([80, 40, 80]); } catch (e) {}
  }

  const statusEl = document.getElementById("scannerStatus");
  if (statusEl) {
    statusEl.innerHTML = '🎉 <strong>QR Code Recognized!</strong> Redirecting to attendance confirmation...';
    statusEl.style.color = "#16a34a";
  }

  // Stop scanner
  stopCameraScanner();

  // If decodedText is a URL, redirect to it
  if (decodedText.startsWith("http://") || decodedText.startsWith("https://") || decodedText.startsWith("/")) {
    window.location.href = decodedText;
  } else if (decodedText.includes("attend/")) {
    // Relative link
    window.location.href = "/" + decodedText.replace(/^\/+/, "");
  } else {
    // Might be raw token or JSON
    showScannerError("Unrecognized QR Code. Please scan the official ACCLLMS projector code.");
  }
}

function onQrCodeProgress(errorMessage) {
  // Silent frame error while searching for QR code in camera frames
}

function showScannerError(msg) {
  const statusEl = document.getElementById("scannerStatus");
  if (statusEl) {
    statusEl.innerHTML = `<span style="color:#dc2626;">⚠️ ${msg}</span>`;
  }
}

// Close scanner with ESC key
document.addEventListener("keydown", function(e) {
  if (e.key === "Escape") {
    closeAttendanceScanner();
  }
});
