() => {
  const accessibleName = element => {
    const labelledBy = element.getAttribute('aria-labelledby');
    const labelledText = labelledBy ? labelledBy.split(/\s+/).map(id => document.getElementById(id)?.textContent?.trim() || '').join(' ').trim() : '';
    return element.getAttribute('aria-label') || labelledText || element.getAttribute('title') || element.textContent?.trim() || element.querySelector('img[alt]')?.getAttribute('alt') || element.querySelector('svg title')?.textContent?.trim() || '';
  };
  const controls = [...document.querySelectorAll('button,a[href],[role="button"]')].filter(element => !accessibleName(element)).length;
  const inputs = [...document.querySelectorAll('input,select,textarea')].filter(element => {
    const id = element.getAttribute('id');
    return !element.getAttribute('aria-label') && !element.getAttribute('aria-labelledby') && !(id && document.querySelector(`label[for="${CSS.escape(id)}"]`));
  }).length;
  return {
    text: document.body.innerText,
    svgCount: document.querySelectorAll('svg').length,
    rectCount: document.querySelectorAll('rect').length,
    canvasCount: document.querySelectorAll('canvas').length,
    headingCount: document.querySelectorAll('h1').length,
    horizontalOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 2,
    accessibilityIssueCount: controls + inputs + [...document.querySelectorAll('img')].filter(element => !element.hasAttribute('alt')).length,
  };
}
