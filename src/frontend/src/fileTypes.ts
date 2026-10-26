// Linked firmware images are ELF files under several names depending on the
// toolchain: .elf, .out, .abs (Renesas CC-RH) and .axf (Arm/Keil). The
// importer identifies an ELF by its content, so the extension only drives
// pickers and the Import wizard's routing.
export const ELF_EXTS = [".elf", ".out", ".abs", ".axf"];

// Files the symbol importer accepts: an ELF image or an exported .json cache.
export const SYMBOL_IMPORT_EXTS = [...ELF_EXTS, ".json"];

export function isSymbolFileName(path: string): boolean {
  const lower = path.toLowerCase();
  return SYMBOL_IMPORT_EXTS.some((e) => lower.endsWith(e));
}

// "vcu_R2.out" → "vcu_R2" (default release name for an imported image).
export function stripSymbolExt(fileName: string): string {
  return fileName.replace(/\.(elf|out|abs|axf|json)$/i, "");
}
