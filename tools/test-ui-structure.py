# -*- coding: utf-8 -*-
"""
PC 端 UI 结构冒烟测试（不需要真掌机）。
========================================

构造真实的 tkinter App（不进入 mainloop），断言：
  · 左栏是"本机"、右栏是"掌机"（问题 1）
  · 本机栏有盘符下拉框，且能枚举出盘符（问题 2）
  · 按钮文案是「发送至电脑」「发送至掌机」（问题 3）
  · 有"请去掌机操作"的醒目提示条 mechanism（问题 5）

用 withdraw() 隐藏窗口，跑完即退，CI 友好。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "pc"))

PASS, FAIL = [], []


def check(name, cond, note=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'[OK]' if cond else '[!!]'}  {name}" + (f"  —— {note}" if note else ""))


def main():
    print("=" * 66)
    print("  PC 端 UI 结构冒烟测试")
    print("=" * 66)

    try:
        import tkinter as tk
    except ImportError as e:
        print(f"!! 本机 Python 无 tkinter：{e}")
        return 2

    import main as pcmain

    # ---- 先测不依赖 tk 的纯函数 ----
    print("1. 盘符枚举（问题 2 的基础设施）")
    drives = pcmain.list_drives()
    check("list_drives() 返回非空", bool(drives), f"{drives}")
    check("每项是 (label, path) 二元组",
          all(isinstance(d, tuple) and len(d) == 2 for d in drives))
    if os.name == "nt":
        check("Windows 上能枚举出盘符（至少有 C:）",
              any(p.rstrip("\\/").upper().startswith("C:")
                  for _, p in drives),
              f"{[d[0] for d in drives]}")
    print()

    # ---- 构造真实 App ----
    root = tk.Tk()
    root.withdraw()             # 不弹窗
    app = pcmain.App(root, host=None)

    print("2. 左右栏归属（问题 1：本机应在左）")
    # 本机树 tree_l 的父容器里，标题应是"本机 (Windows)"
    def find_head_labels(widget):
        out = []
        for child in widget.winfo_children():
            try:
                if child.winfo_class() == "TLabel":
                    t = child.cget("text")
                    if t:
                        out.append(t)
            except Exception:
                pass
            out += find_head_labels(child)
        return out

    labels_all = find_head_labels(root)
    check("界面里有「本机 (Windows)」标题",
          any("本机 (Windows)" in t for t in labels_all))
    check("界面里有「掌机 (RG35XX Pro)」标题",
          any("掌机" in t for t in labels_all))

    # 用 grid 的列号判断左右：tree_l 的祖父容器应在 col 0
    def grid_col_of(widget):
        try:
            info = widget.grid_info()
            return int(info.get("column", -1))
        except Exception:
            return -1

    l_col = grid_col_of(app.tree_l.master)
    r_col = grid_col_of(app.tree_r.master)
    check("★ 本机栏在左（grid column 更小）", l_col < r_col,
          f"本机 col={l_col} 掌机 col={r_col}")
    print()

    print("3. 盘符下拉框（问题 2）")
    check("存在盘符下拉框 cmb_drive", hasattr(app, "cmb_drive"))
    vals = list(app.cmb_drive.cget("values"))
    check("下拉框有候选项", len(vals) > 0, f"{len(vals)} 项")
    if os.name == "nt":
        check("下拉框里含盘符（💽）",
              any("💽" in v for v in vals))
        check("下拉框里含常用目录（📁）",
              any("📁" in v for v in vals))
    print()

    print("4. 按钮文案（问题 3 → 用户反馈 #4 再改）")
    check("存在「复制进电脑」按钮",
          "复制进电脑" in app.btn_to_pc.cget("text"),
          app.btn_to_pc.cget("text"))
    check("存在「复制进掌机」按钮",
          "复制进掌机" in app.btn_to_hh.cget("text"),
          app.btn_to_hh.cget("text"))
    check("箭头方向：复制进电脑朝左（←）",
          "←" in app.btn_to_pc.cget("text"))
    check("箭头方向：复制进掌机朝右（→）",
          "→" in app.btn_to_hh.cget("text"))
    check("★ 不再出现「发送至电脑」字样",
          "发送至电脑" not in app.btn_to_pc.cget("text"))
    check("★ 不再出现「发送至掌机」字样",
          "发送至掌机" not in app.btn_to_hh.cget("text"))
    check("★ 不再出现旧的「下载」字样",
          "下载" not in app.btn_to_pc.cget("text")
          and "下载" not in app.btn_to_hh.cget("text"))
    check("★ 不再出现旧的「上传」字样",
          "上传" not in app.btn_to_pc.cget("text")
          and "上传" not in app.btn_to_hh.cget("text"))
    print()

    print("4b. 文件列表改四列（问题 5）")
    for tag, t in (("本机", app.tree_l), ("掌机", app.tree_r)):
        cols = list(t["columns"])
        check(f"{tag}列表是 4 列（name/ext/size/mtime）",
              cols == ["name", "ext", "size", "mtime"], f"{cols}")
        check(f"{tag}列表「后缀」列存在",
              t.heading("ext")["text"] == "后缀",
              t.heading("ext")["text"])
        check(f"{tag}列表「修改时间」列存在",
              t.heading("mtime")["text"] == "修改时间")
        # 列宽要"合理"：名称最宽、时间次之、后缀最窄
        wn = int(t.column("name")["width"])
        we = int(t.column("ext")["width"])
        ws = int(t.column("size")["width"])
        wm = int(t.column("mtime")["width"])
        check(f"{tag}列表列宽合理（名称>时间>大小>后缀）",
              wn > wm > ws > we,
              f"名称{wn} 时间{wm} 大小{ws} 后缀{we}")
        check(f"{tag}列表「修改时间」列宽够放下 16 字符",
              wm >= 120, f"{wm}px")
    print()

    print("4c. 后缀列取值（问题 5）")
    check("目录显示 DIR", pcmain.file_ext("Roms", True) == "DIR")
    check("a.pdf → pdf", pcmain.file_ext("a.pdf") == "pdf")
    check("大写 .JPG → 小写 jpg", pcmain.file_ext("A.JPG") == "jpg")
    check("多后缀 a.tar.gz → gz", pcmain.file_ext("a.tar.gz") == "gz")
    check("无后缀 → 破折号", pcmain.file_ext("README") == "—")
    check("纯 dotfile .gitignore → 破折号（不是后缀）",
          pcmain.file_ext(".gitignore") == "—")
    check(".env.local → local（多点的 dotfile 取末段）",
          pcmain.file_ext(".env.local") == "local")
    print()

    print("4d. 自动扫描时间窗模型（第四轮反馈 #2）")
    check("有 auto_scan 开关", hasattr(app, "auto_scan"))
    # ★ 第四轮反馈 #2 定稿：废弃"最多 5 遍 + 20 秒间隔"，
    #   改成"启动 1 次 + 每 7 秒 1 次 + 满 5 分钟停"。
    check("★ 扫描间隔为 7 秒",
          app.AUTO_SCAN_INTERVAL_S == 7.0,
          f"{app.AUTO_SCAN_INTERVAL_S}")
    check("★ 自动扫描时间窗为 5 分钟（300 秒）",
          app.AUTO_SCAN_WINDOW_S == 300.0,
          f"{app.AUTO_SCAN_WINDOW_S}")
    check("★ 旧模型属性已移除（max_scan_rounds）",
          not hasattr(app, "max_scan_rounds"))
    check("★ 旧模型属性已移除（scan_gap_s）",
          not hasattr(app, "scan_gap_s"))
    check("有时间窗起点变量 _scan_t0", hasattr(app, "_scan_t0"))
    check("有 _scan_job 句柄（可取消挂起的一拍）",
          hasattr(app, "_scan_job"))
    check("有 _auto_scan_start 方法（启动即扫 + 记起点）",
          hasattr(app, "_auto_scan_start"))
    check("有 _auto_scan_tick 方法", hasattr(app, "_auto_scan_tick"))
    check("有 _auto_scan_next 方法", hasattr(app, "_auto_scan_next"))
    check("有 _stop_auto_scan 方法", hasattr(app, "_stop_auto_scan"))
    check("有 cli 访问锁 cli_lock", hasattr(app, "cli_lock"))
    print()

    print("5. 醒目提示条机制（问题 5）")
    check("App 有 attn_frame 提示条容器", hasattr(app, "attn_frame"))
    check("App 有 _show_attn 方法", hasattr(app, "_show_attn"))
    check("App 有 _on_notice 回调", hasattr(app, "_on_notice"))
    check("Client 有 on_notice 挂点",
          hasattr(pcmain.clipy.Client("127.0.0.1"), "on_notice"))

    # 触发一次提示，确认不抛异常
    try:
        app._show_attn("测试提示：请在掌机上按 A", "confirm")
        app.root.update_idletasks()
        ok_show = True
    except Exception as e:
        ok_show = False
        print(f"      异常: {e}")
    check("_show_attn 能正常显示", ok_show)
    try:
        app._hide_attn()
        ok_hide = True
    except Exception as e:
        ok_hide = False
        print(f"      异常: {e}")
    check("_hide_attn 能正常收起", ok_hide)
    print()

    print("6. 保活机制（问题 4 的 UI 侧）")
    check("App 有 _heartbeat 方法", hasattr(app, "_heartbeat"))
    check("Client 有 keepalive 方法",
          hasattr(pcmain.clipy.Client("127.0.0.1"), "keepalive"))
    print()

    # ---- 7. ★ 第四轮反馈：按钮文案 / 删除按钮 / 双端删除 ----
    print("7. 第四轮反馈：按钮区（#3 新建目录文案  #4 删除按钮）")
    check("★ 新建目录按钮存在", hasattr(app, "btn_mkdir"))
    mk_txt = app.btn_mkdir.cget("text")
    check("★ 新建目录按钮文案点明「掌机当前目录」",
          "掌机当前目录" in mk_txt, f"文案={mk_txt!r}")
    check("★ 新建目录按钮文案不再只是笼统「新建目录」",
          mk_txt.strip() != "新建目录", f"文案={mk_txt!r}")

    check("★ 删除按钮存在", hasattr(app, "btn_del"))
    del_txt = app.btn_del.cget("text")
    check("★ 删除按钮文案提到「掌机文件」",
          "掌机文件" in del_txt, f"文案={del_txt!r}")
    # 无选中项时必须是禁用态 —— 这是"当有掌机文件被选中时该按钮可用"的基线
    app.tree_r.selection_remove(*app.tree_r.selection())
    app._on_remote_select()
    check("★ 无选中项时删除按钮禁用",
          str(app.btn_del.cget("state")) == "disabled",
          f"state={app.btn_del.cget('state')}")

    check("★ 有 _on_remote_select 回调（选中状态驱动按钮）",
          hasattr(app, "_on_remote_select"))
    check("★ 有 do_delete 方法（删除入口）",
          hasattr(app, "do_delete"))
    check("★ 有 _finish_delete 方法（收尾刷新）",
          hasattr(app, "_finish_delete"))
    check("★ tree_r 绑定了 <<TreeviewSelect>>",
          "<<TreeviewSelect>>" in app.tree_r.bind())
    print()

    print("7b. 第四轮反馈：删除协议链路（#4 双端确认）")
    check("★ PC 端 protocol 有 OP_DELETE_BEGIN",
          pcmain.P.OP_DELETE_BEGIN == 0x0031,
          f"0x{pcmain.P.OP_DELETE_BEGIN:04X}")
    check("★ OP_NAME 注册了 DELETE_BEGIN",
          pcmain.P.OP_NAME.get(0x0031) == "DELETE_BEGIN")
    check("★ 新增 NAK_IS_DIR 原因码",
          pcmain.P.NAK_IS_DIR == 6,
          f"{pcmain.P.NAK_IS_DIR}")
    check("★ NAK_TEXT 覆盖 NAK_IS_DIR",
          6 in pcmain.P.NAK_TEXT)
    check("★ Client 有 delete 方法",
          hasattr(pcmain.clipy.Client("127.0.0.1"), "delete"))
    print()

    # ---- 8. ★ 第六轮反馈：配置持久化 / IP 记忆 / 桌面默认目录 / SELECT 同步 ----
    print("8a. 第六轮反馈 #2：掌机 IP 不能再硬编码默认值")
    check("★ 已删除 DEFAULT_HOST 常量",
          not hasattr(pcmain, "DEFAULT_HOST"))
    check("★ 有 load_config 函数", hasattr(pcmain, "load_config"))
    check("★ 有 save_config 函数", hasattr(pcmain, "save_config"))
    check("★ 有 remember_host 函数", hasattr(pcmain, "remember_host"))
    check("★ 配置文件落在 APPDATA 下",
          "PocketTransfer" in pcmain.CONFIG_PATH,
          pcmain.CONFIG_PATH.replace(os.sep, "/")[-48:])
    check("★ 从配置读 last_host 作默认值",
          "last_host" in open(os.path.join(ROOT, "pc", "main.py"),
                              encoding="utf-8").read())
    # 没配置时 host 应为空串（不塞"别人的"IP）
    check("★ 无配置时 host 为空（不是别人局域网的 IP）",
          app.host == "" or app.host == (pcmain.load_config().get("last_host") or ""),
          f"host={app.host!r}")
    check("★ 空 IP 时有 placeholder 挂点 _host_ph",
          hasattr(app, "_host_ph"))
    check("★ 有 _attach_host_placeholder 方法",
          hasattr(app, "_attach_host_placeholder"))
    # 占位文字不能污染 var_host 的真实值
    if not app.host:
        app.var_host.set("")
        app.entry_host.event_generate("<FocusOut>")
        check("★ 占位文字绝不写进 var_host（否则会拿去连接）",
              app.var_host.get() == "",
              f"var_host={app.var_host.get()!r}")
    print()

    print("8b. 第六轮反馈 #3：本机栏默认目录 = 桌面")
    d = pcmain.default_local_dir()
    check("★ default_local_dir() 返回桌面",
          os.path.basename(d).lower() in ("desktop", "桌面"),
          d)
    check("★ 默认目录是个真实存在的目录", os.path.isdir(d))
    check("★ App.local_path 用的是默认目录",
          os.path.abspath(app.local_path) == os.path.abspath(d),
          app.local_path)
    check("★ 下拉框含「桌面」项",
          any("桌面" in t for t, _ in app._drive_items),
          str([t for t, _ in app._drive_items][:4]))
    # 下拉框当前值应已经同步到桌面（因为 local_path 就是桌面）
    check("★ 下拉框当前值已同步到桌面",
          "桌面" in app.var_drive.get() or
          os.path.abspath(app.var_drive.get()) == os.path.abspath(d),
          f"var_drive={app.var_drive.get()!r}")
    print()

    print("8c. 第六轮反馈 #1：SELECT 切读写后 PC 要能同步")
    check("★ protocol 有 NOTICE_PERM 常量",
          getattr(pcmain.P, "NOTICE_PERM", None) == "perm",
          str(getattr(pcmain.P, "NOTICE_PERM", None)))
    check("★ App 有 _apply_perm（主线程刷权限标签）",
          hasattr(app, "_apply_perm"))
    src_main = open(os.path.join(ROOT, "pc", "main.py"),
                    encoding="utf-8").read()
    check("★ _on_notice 里有 perm 分支",
          'kind == "perm"' in src_main)
    # 取 _heartbeat 到下一个顶层 def 之间的正文。
    # ⚠️ 不能用 split("def ")—— _heartbeat 里还嵌着 `def work():`，
    #    会把正文从中间劈开，导致 find 失败（这正是本测试第一版的 bug）。
    import re as _re
    hb = _re.search(r"\n    def _heartbeat\(self\):(.*?)\n    def ",
                    src_main, _re.S)
    hb_body = hb.group(1) if hb else ""
    check("★ 能定位到 _heartbeat 函数体", bool(hb_body))
    check("★ _heartbeat 里会顺带同步权限（_update_perm）",
          "_update_perm" in hb_body)
    check("★ 心跳间隔已提速到 4 秒以内",
          "_last_ka > 4" in hb_body or "_last_ka > 3" in hb_body,
          "阈值=" + (("4" if "_last_ka > 4" in hb_body else "3")
                     if hb_body else "?"))
    ka = _re.search(r"\n    def keepalive\(self\):(.*?)\n    def ",
                    open(os.path.join(ROOT, "pc", "client.py"),
                         encoding="utf-8").read(), _re.S)
    ka_body = ka.group(1) if ka else ""
    check("★ keepalive 改用 GET_DEVICE_INFO（顺带回设备信息）",
          "GET_DEVICE_INFO" in ka_body and "self.info" in ka_body)
    # 掌机侧
    src_net = open(os.path.join(ROOT, "handheld", "net.py"),
                   encoding="utf-8").read()
    check("★ 掌机 set_writable 会主动 notify_peer",
          "notify_peer" in src_net and "set_writable" in src_net)
    check("★ 掌机有 notify_peer 方法", "def notify_peer" in src_net)
    check("★ _serve 主循环是轮询式（POLL_S）",
          "POLL_S" in src_net)
    check("★ 掌机能发出 OP_NOTICE(perm)",
          '"perm"' in src_net or "'perm'" in src_net)
    print()

    print("8d. 版本号已升到 0.3")
    check("★ VER 为 0.3", pcmain.VER == "0.3", pcmain.VER)
    print()

    try:
        root.destroy()
    except Exception:
        pass

    print("=" * 66)
    print(f"  结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    print("=" * 66)
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
        return 1
    print("全部通过：PC 端 UI 结构符合预期。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
