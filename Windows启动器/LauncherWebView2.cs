using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

// ============================================================
//  ye队作品.exe —— 内嵌浏览器窗口的演示程序
//
//  双击后不会打开外部浏览器：
//    1. 后台启动 demo\runtime\python.exe 提供的本地服务
//    2. 用 WebView2 在本窗口内加载面板
//    3. 关窗口即结束后台进程
// ============================================================
static class Launcher
{
    static Process backend;

    [STAThread]
    static void Main()
    {
        string dir = AppDomain.CurrentDomain.BaseDirectory;
        string demo = Path.Combine(dir, "demo");
        string python = Path.Combine(demo, "runtime", "python.exe");

        if (!File.Exists(python))
        {
            MessageBox.Show("找不到 demo\\runtime\\python.exe。\n\n请确认解压时保留了 demo 目录的完整结构。",
                "ye队作品", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }

        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);

        var form = new Form();
        form.Text = "ye队作品 · 可验证的多 Agent 编排";
        var wa = Screen.PrimaryScreen.WorkingArea;
        form.Width = Math.Min(1400, (int)(wa.Width * 0.92));
        form.Height = Math.Min(920, (int)(wa.Height * 0.92));
        form.MinimumSize = new Size(1020, 700);
        form.StartPosition = FormStartPosition.CenterScreen;
        form.BackColor = Color.FromArgb(244, 247, 252);
        try { form.Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { }

        var splash = new Label();
        splash.Text = "正在启动本地服务…";
        splash.Dock = DockStyle.Fill;
        splash.TextAlign = ContentAlignment.MiddleCenter;
        splash.Font = new Font("Microsoft YaHei UI", 12F);
        splash.ForeColor = Color.FromArgb(97, 112, 127);
        form.Controls.Add(splash);

        var web = new WebView2();
        web.Dock = DockStyle.Fill;
        web.Visible = false;
        form.Controls.Add(web);

        form.Shown += async (s, e) =>
        {
            try
            {
                var read = StartBackend(python, demo);
                if (await Task.WhenAny(read, Task.Delay(20000)) != read)
                    throw new Exception("本地服务启动超时");
                var ready = await read;
                int port;
                if (ready == null || !ready.StartsWith("READY ") ||
                    !Int32.TryParse(ready.Substring(6), out port) || port < 1 || port > 65535)
                    throw new Exception("本地服务启动失败，可尝试启动演示.cmd");

                var env = await CoreWebView2Environment.CreateAsync(null, Path.Combine(Path.GetTempPath(), "ye-webview2"));
                await web.EnsureCoreWebView2Async(env);

                // 外部链接交给系统浏览器，避免在演示窗口里跳出站点
                web.CoreWebView2.NewWindowRequested += (a, b) =>
                {
                    b.Handled = true;
                    Uri target;
                    if (Uri.TryCreate(b.Uri, UriKind.Absolute, out target) &&
                        (target.Scheme == "https" || target.Scheme == "http"))
                    {
                        try { Process.Start(target.AbsoluteUri); } catch { }
                    }
                };
                web.CoreWebView2.Settings.AreDevToolsEnabled = false;
                web.CoreWebView2.Settings.IsStatusBarEnabled = false;

                web.CoreWebView2.Navigate("http://127.0.0.1:" + port + "/");
                web.Visible = true;
                splash.Visible = false;
            }
            catch (Exception ex)
            {
                StopBackend();
                splash.Text = "启动失败：" + ex.Message;
            }
        };

        form.FormClosed += (s, e) => StopBackend();
        Application.Run(form);
        StopBackend();
    }

    static Task<string> StartBackend(string python, string demo)
    {
        var psi = new ProcessStartInfo(python, "-B -X utf8 -m tidal_harness serve --demo --port 0");
        psi.WorkingDirectory = demo;
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        psi.RedirectStandardOutput = true;
        var ready = new TaskCompletionSource<string>();
        backend = new Process();
        backend.StartInfo = psi;
        backend.OutputDataReceived += (sender, line) => ready.TrySetResult(line.Data);
        backend.Start();
        backend.BeginOutputReadLine();
        return ready.Task;
    }

    static void StopBackend()
    {
        try
        {
            if (backend != null && !backend.HasExited) backend.Kill();
        }
        catch { }
    }
}
