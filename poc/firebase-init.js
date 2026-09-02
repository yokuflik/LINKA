// Initializes the Firebase compat SDK for Phone Auth (ADR 0009).
// Loaded after the gstatic firebase-app/-auth compat scripts and firebase-config.js.
// Exposes window.firebaseAuth (or null when the SDK / config is unavailable, e.g.
// opened as file:// with no network - the 1..5 dev-whitelist path still works then).
(function () {
  try {
    if (!window.firebase || !window.FIREBASE_CONFIG) {
      window.firebaseAuth = null;
      console.warn('[firebase] SDK or config missing - real phone verification disabled');
      return;
    }
    firebase.initializeApp(window.FIREBASE_CONFIG);
    window.firebaseAuth = firebase.auth();
  } catch (err) {
    window.firebaseAuth = null;
    console.warn('[firebase] init failed - real phone verification disabled:', err);
  }
})();
