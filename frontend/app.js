(() => {
  "use strict";

  document.documentElement.dataset.js = "ready";

  const adminDialog = document.querySelector("#admin-dialog");
  const openAdminButton = document.querySelector("#open-admin");

  if (!adminDialog || !openAdminButton) {
    return;
  }

  openAdminButton.addEventListener("click", () => {
    if (typeof adminDialog.showModal === "function") {
      adminDialog.showModal();
    } else {
      adminDialog.setAttribute("open", "");
    }
  });

  adminDialog.addEventListener("click", (event) => {
    if (event.target === adminDialog) {
      adminDialog.close();
    }
  });
})();
