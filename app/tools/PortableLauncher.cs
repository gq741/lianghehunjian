using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;
class PortableLauncher {
  [STAThread] static int Main() {
    try {
      string root = AppDomain.CurrentDomain.BaseDirectory;
      string python = Path.Combine(root, "runtime", "pythonw.exe");
      string boot = Path.Combine(root, "app", "tools", "portable_boot.py");
      if (!File.Exists(python) || !File.Exists(boot)) throw new Exception();
      ProcessStartInfo start = new ProcessStartInfo(python,
          "-I -B -X utf8 \"" + boot + "\"");
      start.WorkingDirectory = root;
      start.UseShellExecute = false;
      start.CreateNoWindow = true;
      Process.Start(start);
      return 0;
    } catch {
      MessageBox.Show("请完整复制量禾混剪文件夹，并运行免费自检。", "量禾混剪", MessageBoxButtons.OK, MessageBoxIcon.Error);
      return 1;
    }
  }
}
