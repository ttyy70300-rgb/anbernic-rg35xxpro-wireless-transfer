/* fbtest.c — 阶段 0-B：零依赖 framebuffer 测试程序
 *
 * 目的（两个，缺一不可）：
 *   1. 验证 aarch64 交叉编译器可用
 *   2. 验证掌机能执行我们编译的二进制
 *
 * 刻意不依赖 SDL2 —— 固件里 libSDL2.so 的确切路径和版本要等 SSH 抓回来才知道。
 * 直接用 Linux framebuffer 接口画色块，零第三方依赖，是最快的证伪手段。
 *
 * 屏幕预期效果：红 / 绿 / 蓝 三条横向色带。
 *
 * 构建：
 *   zig cc -target aarch64-linux-musl -O2 -static fbtest.c -o fbtest
 */

#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>
#include <linux/fb.h>

/* RGB565 */
#define RGB565(r, g, b) \
    ((unsigned short)((((r) >> 3) << 11) | (((g) >> 2) << 5) | ((b) >> 3)))

int main(void) {
    int fd = open("/dev/fb0", O_RDWR);
    if (fd < 0) {
        perror("open /dev/fb0");
        /* 即使没有 framebuffer 也要证明程序能跑 —— 这是本次测试的核心目的 */
        printf("PocketTransfer fbtest: RAN OK (but /dev/fb0 unavailable)\n");
        fflush(stdout);
        sleep(30);
        return 0;
    }

    struct fb_var_screeninfo vinfo;
    struct fb_fix_screeninfo finfo;

    if (ioctl(fd, FBIOGET_VSCREENINFO, &vinfo) < 0) {
        perror("ioctl FBIOGET_VSCREENINFO");
        close(fd);
        return 1;
    }
    if (ioctl(fd, FBIOGET_FSCREENINFO, &finfo) < 0) {
        perror("ioctl FBIOGET_FSCREENINFO");
        close(fd);
        return 1;
    }

    printf("fb: %ux%u  virtual %ux%u  bpp=%u  line_len=%u\n",
           vinfo.xres, vinfo.yres, vinfo.xres_virtual, vinfo.yres_virtual,
           vinfo.bits_per_pixel, finfo.line_length);
    printf("fb: red_offset=%u green_offset=%u blue_offset=%u\n",
           vinfo.red.offset, vinfo.green.offset, vinfo.blue.offset);

    /* RG35XX Pro 实测：fb0 是 640x480, bpp=32, red=16 green=8 blue=0（BGRA/XRGB8888），
       virtual 640x960（双缓冲）。这里不写死 16bpp，按 vinfo 实测值走。 */

    size_t len = (size_t)finfo.line_length * vinfo.yres;
    unsigned char *fb = mmap(NULL, len, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (fb == MAP_FAILED) {
        perror("mmap framebuffer");
        close(fd);
        return 1;
    }

    const unsigned short bands[3] = {
        RGB565(255, 0, 0),   /* 红 */
        RGB565(0, 255, 0),   /* 绿 */
        RGB565(0, 0, 255),   /* 蓝 */
    };

    if (vinfo.bits_per_pixel == 16) {
        unsigned short *px = (unsigned short *)fb;
        int line_px = finfo.line_length / 2;
        for (int y = 0; y < (int)vinfo.yres; y++) {
            int band = y * 3 / vinfo.yres;
            if (band > 2) band = 2;
            for (int x = 0; x < (int)vinfo.xres; x++)
                px[y * line_px + x] = bands[band];
        }
    } else if (vinfo.bits_per_pixel == 32) {
        /* 按 vinfo 报告的实际通道偏移组像素，不假设固定 BGRA 顺序 */
        unsigned int line_px = finfo.line_length / 4;
        for (int y = 0; y < (int)vinfo.yres; y++) {
            int band = y * 3 / vinfo.yres;
            if (band > 2) band = 2;
            unsigned char r, g, b;
            if (band == 0)      { r = 255; g = 0;   b = 0;   }
            else if (band == 1) { r = 0;   g = 255; b = 0;   }
            else                { r = 0;   g = 0;   b = 255; }

            for (int x = 0; x < (int)vinfo.xres; x++) {
                unsigned int px = fb[y * line_px + x];       /* 读当前值保留 alpha */
                px &= ~(0xFFu << vinfo.red.offset);
                px &= ~(0xFFu << vinfo.green.offset);
                px &= ~(0xFFu << vinfo.blue.offset);
                px |= ((unsigned int)r) << vinfo.red.offset;
                px |= ((unsigned int)g) << vinfo.green.offset;
                px |= ((unsigned int)b) << vinfo.blue.offset;
                fb[y * line_px + x] = px;
            }
        }
    } else {
        printf("fbtest: unsupported bpp=%u\n", vinfo.bits_per_pixel);
        munmap(fb, len);
        close(fd);
        return 2;
    }

    printf("PocketTransfer fbtest: OK — 3 color bands drawn (%dx%d)\n",
           vinfo.xres, vinfo.yres);
    printf("Holding frame for 300s, then exiting.\n");
    fflush(stdout);

    /* 留在屏幕上让用户肉眼确认 */
    sleep(300);

    munmap(fb, len);
    close(fd);
    printf("fbtest: clean exit\n");
    return 0;
}
