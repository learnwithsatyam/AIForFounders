// ---------------------------------------------------------------------------
// rehype plugin: wrap every word of the answer in <span class="tok">.
//
// This is what makes words fade in individually as they stream. Because React
// reuses the spans it already rendered, only the spans appended on this frame
// are freshly mounted — and CSS runs the fade-in animation on mount, so exactly
// the new words animate and settled text stays put.
//
// Code blocks are left untouched: whitespace there is meaningful.
// ---------------------------------------------------------------------------

const SKIP = new Set(['code', 'pre', 'script', 'style'])

export default function rehypeWordSpans() {
  return (tree) => walk(tree)
}

function walk(node) {
  if (!node.children?.length) return
  if (node.type === 'element' && SKIP.has(node.tagName)) return

  const out = []
  for (const child of node.children) {
    if (child.type === 'text') {
      out.push(...splitWords(child.value))
    } else {
      walk(child)
      out.push(child)
    }
  }
  node.children = out
}

function splitWords(value) {
  const nodes = []
  for (const part of value.split(/(\s+)/)) {
    if (!part) continue
    if (/^\s+$/.test(part)) {
      nodes.push({ type: 'text', value: part })
    } else {
      nodes.push({
        type: 'element',
        tagName: 'span',
        properties: { className: ['tok'] },
        children: [{ type: 'text', value: part }],
      })
    }
  }
  return nodes
}
