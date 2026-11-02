/** A made-up agent face as an inline image, so the mock build ships no picture files. */
export const face = (n: number) => {
  const hue = (n * 47) % 360
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" fill="hsl(${hue} 55% 82%)"/><circle cx="32" cy="26" r="11" fill="hsl(${hue} 45% 38%)"/><path d="M10 64c2-16 12-22 22-22s20 6 22 22z" fill="hsl(${hue} 45% 38%)"/></svg>`
  return `data:image/svg+xml;base64,${btoa(svg)}`
}
