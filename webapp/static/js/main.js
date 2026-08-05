const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

for (const board of document.querySelectorAll("[data-board]")) {
  const fragment = document.createDocumentFragment();
  for (let index = 0; index < 64; index += 1) {
    const cell = document.createElement("span");
    const row = Math.floor(index / 8);
    const column = index % 8;
    if ((row + column) % 2) cell.className = "is-dark";
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

const systemScroll = document.querySelector("[data-system-scroll]");
if (systemScroll) {
  const nodes = [...systemScroll.querySelectorAll("[data-stage]")];
  const line = systemScroll.querySelector("[data-pipeline-progress]");
  const ticket = systemScroll.querySelector("[data-message-ticket]");
  const copy = systemScroll.querySelector("[data-stage-copy]");
  const stageCopy = [
    "A PGN source becomes a durable queued job.",
    "SAN moves publish in order under one stable game key.",
    "Go rebuilds the position and measures each move.",
    "Move features collapse into one schema-aligned game row.",
    "PyTorch applies the training L2 transform, then sigmoid.",
    "XGBoost applies saved training minima and ranges.",
    "The joined scores and browser event commit together.",
  ];
  let systemFrame = 0;
  const updateSystem = () => {
    systemFrame = 0;
    const rect = systemScroll.getBoundingClientRect();
    const travel = Math.max(1, rect.height - window.innerHeight);
    const progress = Math.max(0, Math.min(1, -rect.top / travel));
    const active = Math.min(nodes.length - 1, Math.floor(progress * nodes.length));
    nodes.forEach((node, index) => node.classList.toggle("is-active", index <= active));
    line.style.transform = `scaleX(${progress})`;
    if (!reducedMotion.matches) ticket.style.transform = `translate3d(${progress * 48}vw, ${Math.sin(progress * Math.PI) * -34}px, 0)`;
    copy.textContent = stageCopy[active];
  };
  const requestSystem = () => {
    if (!systemFrame) systemFrame = requestAnimationFrame(updateSystem);
  };
  window.addEventListener("scroll", requestSystem, { passive: true });
  updateSystem();
}
