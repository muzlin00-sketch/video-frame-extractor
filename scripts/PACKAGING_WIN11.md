# VideoAnalyzer 安装和使用说明（稳定版）

## 1. 给分发方：生成安装包

在 `scripts` 目录执行一条命令：

```powershell
powershell -ExecutionPolicy Bypass -File .\build_setup_win11.ps1
```

生成文件：

```text
scripts\dist\VideoAnalyzer_Setup.exe
```

把这个 `VideoAnalyzer_Setup.exe` 发给最终用户即可。

## 2. 给最终用户：安装

1. 双击 `VideoAnalyzer_Setup.exe`
2. 一路“下一步”完成安装
3. 可选勾选“创建桌面快捷方式”

默认安装目录：

```text
%LOCALAPPDATA%\VideoAnalyzer
```

## 3. 第一次启动（必须做）

1. 从桌面或开始菜单打开 `VideoAnalyzer`
2. 程序会自动打开浏览器页面（默认 `http://127.0.0.1:7860`）
3. 进入“设置”，填写并保存：
   - `api_key`
   - `base_url`
   - `model`

配置会保存到：

```text
安装目录\app_config.json
```

## 4. 日常使用流程

1. 上传视频文件
2. 选择分析模式
3. 点击“开始分析”
4. 下载 TXT / JSON 报告

## 5. 常见问题（稳定使用）

- 页面没自动打开：手动访问 `http://127.0.0.1:7860`
- 7860 端口被占用：关闭占用程序后重启本软件
- 启动后立即退出：右键“以管理员身份运行”再试一次
- 看不到分析结果：先在设置里确认 `api_key/base_url/model` 已保存
