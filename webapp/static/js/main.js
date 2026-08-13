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
