#!/usr/bin/env python3
"""build-test-package.py — 阶段 0-B：生成测试部署包

按 v1.2 修正后的结构打包（这是原厂固件唯一能识别的结构）：

    Roms/APPS/
    ├── fbtest.sh          ← 启动脚本，直接在 APPS 根目录
    ├── PocketTransfer/    ← 负载目录（脚本的兄弟目录）
    │   └── fbtest         ← aarch64 可执行文件
    └── Imgs/
        └── fbtest.png     ← 菜单图标

关键：原厂 launcher 只扫描 Roms/APPS 下的 *.sh，目录不会被列为应用。
若把脚本和二进制都塞进一个目录里，Apps Center 里不会出现入口。
"""

import argparse
import os
import shutil
import stat
import sys
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_exe(path: int) -> None:
    """Linux 可执行权限位 —— FAT32 会保留，但保险起见显式设置。"""
    st = os.stat(path)
    os.chmod(path, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def check_lf(path: str) -> None:
    """确保 shell 脚本是纯 LF 换行。

    Windows 上编辑的 .sh 常带 CRLF，拷到 Linux 上执行会报
    `bad interpreter: /bin/bash^M` —— 这是阶段 0 最容易踩且最难查的坑。
    """
    with open(path, "rb") as f:
        data = f.read()
    if b"\r\n" in data:
        # 直接原地修正，不只是报错
        with open(path, "wb") as f:
            f.write(data.replace(b"\r\n", b"\n"))
        print(f"  [修正] {os.path.basename(path)} 的 CRLF 已转为 LF")
    if not data.startswith(b"#!"):
        print(f"  [警告] {os.path.basename(path)} 缺少 shebang 行")


def build(binary: str, outdir: str) -> str:
    """把已编译的 aarch64 二进制组装成可拷卡的目录树。"""
    src_bin = os.path.join(REPO, binary)
    if not os.path.isfile(src_bin):
        sys.exit(f"[错误] 找不到编译产物: {src_bin}")

    apps = os.path.join(outdir, "Roms", "APPS")
    payload = os.path.join(apps, "PocketTransfer")
    imgs = os.path.join(apps, "Imgs")
    for d in (payload, imgs):
        os.makedirs(d, exist_ok=True)

    # 1) 启动脚本 -> APPS 根目录（必须是根目录！）
    launcher = os.path.join(REPO, "handheld", "test", "fbtest.sh")
    check_lf(launcher)
    dst_sh = os.path.join(apps, "fbtest.sh")
    shutil.copy2(launcher, dst_sh)
    os.chmod(dst_sh, os.stat(dst_sh).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # 2) 二进制 -> 负载目录
    dst_bin = os.path.join(payload, "fbtest")
    shutil.copy2(src_bin, dst_bin)
    make_exe(dst_bin)

    # 3) 占位图标（阶段 0 不影响启动，Apps Center 有默认图标）
    icon = os.path.join(imgs, "fbtest.png")
    if not os.path.exists(icon):
        shutil.copyfile(os.path.join(REPO, "handheld", "assets", "icon.png"), icon)

    # 4) 预建 logs 目录，避免 FAT32 上 mkdir 的意外
    os.makedirs(os.path.join(payload, "logs"), exist_ok=True)

    return outdir


def make_zip(outdir: str) -> str:
    """打包成 zip。

    Windows 上 zipfile 不会保留可执行位，这里显式补上 ——
    解压到 Linux 后直接 ./fbtest 才不会报 Permission denied。
    （launcher.sh 里有 chmod +x 兜底，但包本身正确更省心）
    """
    zpath = os.path.join(outdir, "fbtest_Roms.zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _dirs, files in os.walk(outdir):
            for fn in files:
                full = os.path.join(root, fn)
                if full.endswith(".zip"):
                    continue
                # zip 内部一律用正斜杠；Windows 上 os.path 会给反斜杠
                rel = os.path.relpath(full, outdir).replace(os.sep, "/")
                z.write(full, rel)
                if fn.endswith(".sh") or fn == "fbtest":
                    # 0o100755：普通文件 + 可执行位
                    z.getinfo(rel).external_attr = 0o100755 << 16
    return zpath


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", default="handheld/build/fbtest",
                    help="已编译的 aarch64 二进制路径（相对仓库根）")
    ap.add_argument("--out", default=os.path.join(REPO, "dist-test"),
                    help="输出目录")
    args = ap.parse_args()

    if os.path.isdir(args.out):
        shutil.rmtree(args.out)

    build(args.bin, args.out)
    z = make_zip(args.out)

    print("测试包已生成：")
    for root, _dirs, files in os.walk(args.out):
        for fn in sorted(files):
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, args.out)
            size = os.path.getsize(full)
            print(f"  {size:>9}  {rel}")
    print(f"\n拷贝步骤：把 {os.path.join(args.out, 'Roms')} 下的内容拷进 TF 卡根目录")
    print(f"或直接解压 zip 到卡根：{z}")


if __name__ == "__main__":
    main()
