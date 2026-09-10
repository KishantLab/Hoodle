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

  const readerDiv = document.getElementById("qrReader");
  if (readerDiv) readerDiv.style.display = "block";

  const laser = document.querySelector(".scanner-laser-line");
  if (laser) laser.style.display = "block";

  const statusEl = document.getElementById("scannerStatus");
  if (statusEl) {
    statusEl.innerHTML = '<span class="pulse-dot" style="width:8px;height:8px;"></span> Starting camera...';
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
}

function stopCameraScanner() {
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

  const prefix = window.location.pathname.startsWith("/lms") ? "/lms" : "";

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

// Display step-by-step guidance for Android and iOS when camera permission fails
function showCameraPermissionGuide(errorMsg) {
  const laser = document.querySelector(".scanner-laser-line");
  if (laser) laser.style.display = "none";

  const readerDiv = document.getElementById("qrReader");
  if (readerDiv) readerDiv.style.display = "none";

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
