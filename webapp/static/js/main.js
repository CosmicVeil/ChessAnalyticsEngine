const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
const preferredLightTheme = window.matchMedia("(prefers-color-scheme: light)");
const themeToggle = document.querySelector("[data-theme-toggle]");
const themeLabel = document.querySelector("[data-theme-label]");
const themeColor = document.querySelector('meta[name="theme-color"]');

function applyTheme(theme, persist = false) {
  const isLight = theme === "light";
  document.documentElement.dataset.theme = isLight ? "light" : "dark";
  themeToggle?.setAttribute("aria-pressed", String(isLight));
  themeToggle?.setAttribute("aria-label", `Switch to ${isLight ? "dark" : "light"} mode`);
  if (themeLabel) themeLabel.textContent = `${isLight ? "Dark" : "Light"} mode`;
  themeColor?.setAttribute("content", isLight ? "#f5f7fa" : "#0a0c10");
  if (persist) localStorage.setItem("cae-theme", isLight ? "light" : "dark");
}

applyTheme(document.documentElement.dataset.theme || (preferredLightTheme.matches ? "light" : "dark"));
themeToggle?.addEventListener("click", () => {
  applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light", true);
});
preferredLightTheme.addEventListener("change", (event) => {
  if (!localStorage.getItem("cae-theme")) applyTheme(event.matches ? "light" : "dark");
});

const menuButton = document.querySelector("[data-menu-button]");
const sidebar = document.querySelector(".sidebar");
menuButton?.addEventListener("click", () => {
  const open = sidebar.classList.toggle("is-open");
  menuButton.setAttribute("aria-expanded", String(open));
});

const boardPieces = new Map([
  [3, "♛"], [6, "♜"], [11, "♟"], [18, "♞"], [21, "♝"],
  [27, "♟"], [34, "♘"], [36, "♙"], [45, "♙"], [52, "♙"],
  [57, "♖"], [60, "♔"], [62, "♘"],
]);
const boardPieceOrder = [...boardPieces.keys()];
const candidateSquares = [10, 19, 28, 37, 46];

for (const board of document.querySelectorAll("[data-board]")) {
  const fragment = document.createDocumentFragment();
  for (let index = 0; index < 64; index += 1) {
    const cell = document.createElement("span");
    const row = Math.floor(index / 8);
    const column = index % 8;
    if ((row + column) % 2) cell.className = "is-dark";
    if (boardPieces.has(index)) {
      cell.classList.add("has-piece");
      cell.dataset.piece = boardPieces.get(index);
      cell.style.setProperty("--piece-order", String(boardPieceOrder.indexOf(index)));
    }
    if (candidateSquares.includes(index)) {
      cell.classList.add("is-candidate");
      cell.style.setProperty("--candidate-order", String(candidateSquares.indexOf(index)));
    }
    if (index === 34) cell.classList.add("is-active-square");
    fragment.append(cell);
  }
  board.append(fragment);
}

const revealObserver = new IntersectionObserver((entries) => {
  for (const entry of entries) {
    if (entry.isIntersecting) {
      entry.target.classList.add("is-visible");
      revealObserver.unobserve(entry.target);
    }
  }
}, { threshold: 0.14 });
document.querySelectorAll(".reveal").forEach((node) => revealObserver.observe(node));

const depthLayers = [...document.querySelectorAll("[data-depth]")];
let scrollFrame = 0;
function updateDepth() {
  scrollFrame = 0;
  if (reducedMotion.matches) return;
  const center = window.innerHeight / 2;
  for (const layer of depthLayers) {
    const rect = layer.closest(".scroll-scene")?.getBoundingClientRect() ?? layer.getBoundingClientRect();
    const depth = Number(layer.dataset.depth || 0);
    const offset = Math.max(-180, Math.min(180, (rect.top + rect.height / 2 - center) * depth));
    layer.style.setProperty("--layer-y", `${offset.toFixed(2)}px`);
  }
}
function requestDepth() {
  if (!scrollFrame) scrollFrame = requestAnimationFrame(updateDepth);
}
window.addEventListener("scroll", requestDepth, { passive: true });
window.addEventListener("resize", requestDepth);
updateDepth();

for (const stage of document.querySelectorAll("[data-parallax]")) {
  stage.addEventListener("pointermove", (event) => {
    if (reducedMotion.matches) return;
    const rect = stage.getBoundingClientRect();
    const x = ((event.clientX - rect.left) / rect.width - .5) * 18;
    const y = ((event.clientY - rect.top) / rect.height - .5) * 14;
    stage.style.transform = `translate3d(${x}px, ${y}px, 0)`;
  });
  stage.addEventListener("pointerleave", () => { stage.style.transform = ""; });
}

const infoScroll = document.querySelector("[data-info-scroll]");
if (infoScroll) {
  const cards = [...infoScroll.querySelectorAll("[data-info-card]")];
  const previews = [...infoScroll.querySelectorAll("[data-info-preview]")];
  const nodes = [...infoScroll.querySelectorAll("[data-info-node]")];
  let infoFrame = 0;

  const updateInfo = () => {
    infoFrame = 0;
    if (reducedMotion.matches) return;

    const rect = infoScroll.getBoundingClientRect();
    const travel = Math.max(1, rect.height - window.innerHeight);
    const entryOffset = Math.min(window.innerHeight * .25, Math.max(0, infoScroll.offsetTop));
    const progress = Math.max(0, Math.min(1, (entryOffset - rect.top) / travel));
    const stagger = previews.length > 1 ? .68 / (previews.length - 1) : 0;

    previews.forEach((preview, index) => {
      const localProgress = Math.max(0, Math.min(1, (progress - (index * stagger)) / .32));
      preview.style.setProperty("--info-progress", localProgress.toFixed(3));
    });

    const activeIndex = Math.min(nodes.length - 1, Math.floor(progress * nodes.length));
    nodes.forEach((node, index) => node.classList.toggle("is-active", index <= activeIndex));
    cards.forEach((card, index) => card.classList.toggle("is-current", index === activeIndex));
  };

  const requestInfo = () => {
    if (!infoFrame) infoFrame = requestAnimationFrame(updateInfo);
  };

  const handleMotionPreference = () => {
    if (reducedMotion.matches) {
      previews.forEach((preview) => preview.style.removeProperty("--info-progress"));
      nodes.forEach((node) => node.classList.add("is-active"));
      cards.forEach((card) => card.classList.remove("is-current"));
      return;
    }
    requestInfo();
  };

  window.addEventListener("scroll", requestInfo, { passive: true });
  window.addEventListener("resize", requestInfo);
  reducedMotion.addEventListener("change", handleMotionPreference);
  handleMotionPreference();
}
