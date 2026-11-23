export async function copyDiagnosticId(id: string, field: HTMLElement | null): Promise<string> {
  try {
    await navigator.clipboard.writeText(id)
    return '诊断编号已复制'
  } catch {
    // 浏览器禁用剪贴板时，选中仍在页面上的编号以便手动复制。
    const selection = window.getSelection()
    if (!field?.isConnected || !selection) return '请手动复制诊断编号'
    const range = document.createRange()
    range.selectNodeContents(field)
    selection.removeAllRanges()
    selection.addRange(range)
    return '编号已选中，可手动复制'
  }
}
