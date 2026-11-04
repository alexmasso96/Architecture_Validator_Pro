// Turn flat "folder/sub/file.c" rel-paths into a collapsible VS Code-style
// tree (shared by Test Code Injection and the AI Chat explorer).
export interface TreeNode {
  name: string;
  path: string; // file: the full rel_path; dir: the accumulated folder path
  isDir: boolean;
  children: TreeNode[];
}

export function buildFileTree(paths: string[]): TreeNode[] {
  const root: TreeNode = { name: "", path: "", isDir: true, children: [] };
  for (const p of paths) {
    const parts = p.split("/").filter(Boolean);
    let node = root;
    let acc = "";
    parts.forEach((part, i) => {
      acc = acc ? `${acc}/${part}` : part;
      const isFile = i === parts.length - 1;
      let child = node.children.find(
        (c) => c.name === part && c.isDir === !isFile,
      );
      if (!child) {
        child = { name: part, path: isFile ? p : acc, isDir: !isFile, children: [] };
        node.children.push(child);
      }
      node = child;
    });
  }
  const sortRec = (n: TreeNode) => {
    n.children.sort((a, b) =>
      a.isDir === b.isDir ? a.name.localeCompare(b.name) : a.isDir ? -1 : 1,
    );
    n.children.forEach(sortRec);
  };
  sortRec(root);
  return root.children;
}
