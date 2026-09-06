/** @template {keyof HTMLElementTagNameMap} K @param {K} tag @param {string} [text] */
export function node(tag, text = "") {
  const result = document.createElement(tag);
  result.textContent = text;
  return result;
}
/** @param {any[]|undefined|null} blocks */
export function textBlocks(blocks) {
  return (blocks || [])
    .filter((block) => block.type === "text")
    .map((block) => block.text)
    .join("\n");
}
