(function (window, document) {
  "use strict";
  var allowed = ["classic", "bento", "swiss", "cyber"];
  var state = { theme: "classic", saved: false, issue: null, failure: null, storage: null };
  try {
    state.storage = window.localStorage;
    if (!state.storage) { throw new Error("storage_unavailable"); }
  } catch (error) {
    state.issue = "read";
    state.failure = "storage_object";
  }
  if (state.storage) {
    try {
      var value = state.storage.getItem("cloudhost.theme");
      if (value !== null) {
        if (allowed.indexOf(value) !== -1) {
          state.theme = value;
          state.saved = true;
        } else {
          state.issue = "invalid";
        }
      }
    } catch (error) {
      state.issue = "read";
      state.failure = "getItem";
    }
  }
  document.documentElement.setAttribute("data-theme", state.theme);
  document.documentElement.style.colorScheme = state.theme === "swiss" ? "light" : "dark";
  window.CloudhostThemeBoot = state;
})(window, document);
