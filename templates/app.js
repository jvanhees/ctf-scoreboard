// graph links open in a popup on the dashboard; reload pauses while it is open, or it would close it
if (top !== self) {
  document.documentElement.classList.add("embed");
  addEventListener("keydown", e => { if (e.key === "Escape") parent.document.querySelector("dialog").close(); });
}
const dlg = document.querySelector("dialog");
const clock = document.querySelector(".clock");
if (clock) {
  // server time, so a viewer with a wrong device clock still sees the round boundaries right
  const skew = clock.dataset.now - Date.now(), round = clock.dataset.round * 1000;
  const tick = () => {
    const now = new Date(Date.now() + skew), left = round - now % round, secs = Math.ceil(left / 1000);
    clock.querySelector(".time").textContent = now.toLocaleTimeString("en-GB");
    clock.querySelector("b").textContent = `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}`;
    clock.querySelector(".progress div").style.width = `${100 - left / round * 100}%`;
  };
  tick();
  setInterval(tick, 1000);
}
setInterval(() => { if (!dlg || !dlg.open) location.reload(); }, 10000);
document.addEventListener("click", e => {
  const a = e.target.closest('a[href^="/graph"]');
  if (dlg && a) { e.preventDefault(); dlg.querySelector("iframe").src = a.href; dlg.showModal(); }
  else if (e.target === dlg || e.target.closest("dialog button")) dlg.close();
});
