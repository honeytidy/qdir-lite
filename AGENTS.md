# QDir-Lite 开发经验（AGENTS.md）

极简多窗格（2x2）Windows 文件管理器，纯 Python + tkinter + Win32 ctypes，无第三方依赖。
主程序 `qdir_lite.py`，系统图标模块 `fileicons.py`，原生右键菜单模块 `shellmenu.py`。

## 构建与验证

```bash
python -m py_compile qdir_lite.py fileicons.py          # 语法检查
.venv/Scripts/python.exe make_icon.py                   # 生成 app.ico（仅图标图案改动后才需重跑）
.venv/Scripts/pyinstaller.exe QDirLite.spec --noconfirm --clean   # 打包 dist/QDirLite.exe
./dist/QDirLite.exe & sleep 4; tasklist //FI "IMAGENAME eq QDirLite.exe"   # 冒烟验证
```

- PyInstaller 在 `.venv` 里（6.x）；spec 单文件、无控制台
- `hiddenimports=['shellmenu']` 因为它是函数内延迟 import；**顶层 import 的模块（如 fileicons）会被自动分析，不用加**
- 冒烟验证注意：`taskkill //F` 强杀进程不会触发 WM_DELETE_WINDOW，属于预期
- exe 图标：`make_icon.py` 用纯标准库生成 `app.ico`（16~256 七档尺寸，PNG 压缩块），图案与 `make_app_icon()` 一致；spec 里 `icon='app.ico'` 嵌入
- 图标等视觉改动：用脚本把多个图标拼成一张 PNG 再读图目检（`tk.PhotoImage.write(..., format='png')` + `zoom()` 放大）；示意性小图标（如布局切换按钮）直接用 `PhotoImage.put()` 像素级绘制，零资源文件

## Win32 / tkinter 坑（本项目实测踩过）

1. **`DrawIconEx` 没有 W 后缀版本**——它不是字符串 API，`ctypes.windll.user32.DrawIconExW` 会报 function not found
2. **`SHGFI_USEFILEATTRIBUTES` 不解析程序关联**：拿"和资源管理器一致"的图标必须用真实文件路径调 `SHGetFileInfoW`（不带该标志），否则很多类型只返回通用空白页图标；`.exe/.lnk/.ico` 和目录按路径缓存，普通文件按扩展名缓存
3. **HICON → tk.PhotoImage**：绘制到 32bpp top-down DIB（`CreateDIBSection`，`biHeight` 取负值），读出的 BGRA 是**预乘 alpha**，转 PNG 前要做反预乘（`c*255//a`），否则透明边缘发黑
4. **系统默认 UI 字体**用 `SystemParametersInfoW(SPI_GETNONCLIENTMETRICS=0x29)` 的 `lfMessageFont`（中文系统是 Microsoft YaHei UI），磅值 = `-lfHeight * 72 / DPI`；不要写死 Segoe UI
5. **`tk.PhotoImage` 缓存绑定 Tk root**：跨 root（如同进程测试里 destroy 后重建）缓存的 PhotoImage 会失效（`image "pyimageN" doesn't exist`）。正常单进程单 root 没问题，写测试时用独立进程
6. **DPI**：启动时 `SetProcessDpiAwareness(2)`，像素尺寸统一过 `S()`（按 `GetDpiForSystem()/96` 缩放），字体用磅值交给 `tk scaling`；`SM_CXSMICON` 在 DPI 感知进程下自动返回缩放后的图标尺寸
7. 恢复窗口位置时做**离屏兜底**（坐标完全超出 vroot 就忽略），防止换显示器后窗口不可见
8. 窗口最大化时 `geometry()` 不可靠：用 `<Configure>` 实时记录 normal 状态的尺寸存 `_saved_geometry`
9. **shellmenu.py 宿主右键菜单**：子菜单文字空白 = 没转发菜单消息给 `IContextMenu2::HandleMenuMsg`（PS7 这类扩展在 WM_INITMENUPOPUP 时才填文字），需子类化属主窗口转发；Win11"在终端中打开"是现代菜单项拿不到，已用自定义菜单项（顶部"在终端中打开"，wt→PowerShell→cmd 回退）替代
10. **PanedWindow 容器必须先于子窗格创建**：Pane 先于 PanedWindow 创建会导致整窗不渲染（tk 报 mapped/viewable=1 但屏幕全白）。布局切换用 `forget`/`add` 重排，**绝不 destroy 容器重建**
11. **ctypes 调返回句柄/指针的 Win32 API（GlobalAlloc/GlobalLock/SetClipboardData 等）必须显式声明 restype/argtypes**，默认 int 会在 64 位下截断句柄；fileicons.py 曾因此翻车——`CreateCompatibleDC` 等 GDI 句柄超过 2^31 时传回 `CreateDIBSection` 直接抛 `OverflowError`，间歇性抓图标失败回退成 emoji（表现为"文件夹图标忽黄忽白""ico 文件名离图标很远"），已全部补上声明；文件剪贴板用 `CF_HDROP`（DROPFILES 头 20 字节 + 双 \0 结尾的 UTF-16 路径列表）+ `Preferred DropEffect`（1=复制 2=剪切），与资源管理器互通
12. **图标后台异步加载**：`fileicons` 拆成 `fetch_icons`（SHGetFileInfoW+DIB→PNG 字节，后台线程，不碰 tk）和 `peek_photo`（主线程 PNG→PhotoImage）。`_fill` 先无图标插入全部行（几百项约 0.05s），未命中缓存的 key 进 `_icon_jobs` 队列，worker 抓完经 `_icon_done` 由 `QDirLite._poll_icons`（after 80ms 轮询）调 `pane._apply_icons` 逐行补图；用 `_icon_gen` 代次号丢弃过期结果。**tk.PhotoImage 只能在主线程创建**，worker 线程只碰 Win32
13. **剪切/粘贴后刷新所有受影响窗格**（目标目录 + 各源目录所在窗格），否则源窗格还显示已移走的文件；`refresh()` 开头对"目录已不存在"向上回退到最近祖先（浏览中的目录被剪切走时不报错）
14. **外部改动自动刷新**：`_poll_dirs` 每 2s 对比各窗格目录 mtime（目录项增删改会改 mtime），变了就 refresh；`refresh()` 同目录刷新时保留滚动位置和选中项（靠 `_last_refresh_path` 判断），避免自动刷新打断浏览
15. **粘贴放后台线程**（`_paste_worker` → `_paste_done` 队列 → `_poll_bg` 主线程收尾），大文件复制不卡 UI；`_paste_busy` 防重入；**弹 messagebox 只能在主线程**，worker 只收集错误
16. **剪切项置灰**：全局 `_CUT_PATHS` + tree tag `cut`（灰前景），`_fill` 和 `apply_cut_tags()` 两处同步；Ctrl+C 或剪切粘贴完成后清除
17. **目录空白处右键（"新建"子菜单）**：对选中项 `GetUIObjectOf` 拿到的是项目菜单，永远没有"新建"；但直接弹 `CreateViewObject(IID_IContextMenu)`（vtable 8，先 `SHBindToObject` 目录本身；`GetUIObjectOf(cidl=0)` 实测 E_INVALIDARG）的背景菜单会让 PS7 等**第三方扩展子菜单全空白**——该聚合对象不路由 WM_INITMENUPOPUP。最终方案 = **合并**：主菜单用目录作为子项的项目菜单（扩展转发正常），另把背景菜单用独立 id 段（0x8000~0xDFFF）QueryContextMenu 后 `RemoveMenu` 拆出"新建"子菜单 `InsertMenu(MF_POPUP)` 插进主菜单；菜单消息**向两个 pcm 都转发**（实测互不干扰）；invoke 按 id 段选 pcm，背景侧必须带 `lpDirectoryW` + `fMask=CMIC_MASK_UNICODE`。`CDefFolderMenu_Create2`/`SHCreateDefaultContextMenu` 都试过：cidl=0 时要么空菜单要么丢静态 verb，不可用
18. **重命名**：原生菜单要加 `CMF_CANRENAME` 标志才有"重命名"项；shell 的 `rename` verb 在没有 IShellView 的程序里 invoke 是**空操作**，须用 `GetCommandString(GCS_VERBA)` 识别后自实现行内改名（Entry 覆盖 `tree.bbox(iid,"#0")` 文字区，文件只选中主名）。坑：该聚合对象 GCS_VERBW 错误返回帮助文本，**GCS_VERBA 实际按 UTF-16 写规范名**（rename/delete/properties）；新建后进入改名 = 菜单执行前后 diff 目录项找出新文件；慢双击改名 = `_on_press_1` 记录上次点击，间隔 > `GetDoubleClickTime()` 且唯一选中的同一行再次被单击（无拖动）才触发。测试注意：tk `event_generate("<Return>")` 不触发 "<Return>" 绑定，要用 `"<KeyPress-Return>", when="now"` 且先 `focus_force()`
19. **删除只读文件报 WinError 5**：git 的 `.git/objects/pack/*` 等只读文件直接 `os.remove`/`shutil.rmtree` 必拒绝访问；`_menu_delete` 用 `rmtree(onexc=...)`（3.12+）清只读属性后重试，单文件捕获 `PermissionError` 后 `chmod 0o666` 再删

## 界面约定（用户已定型的偏好，勿回退）

- 行高 18（S 缩放）、字号 = 系统默认；列表文字默认黑色，不做类型着色（类型区分靠系统图标）
- 标题栏（按钮+地址栏）**所有窗格默认全隐藏**（`pack_forget`），Ctrl+左键唤出，回车跳转后/Esc/焦点转移自动收起；当前路径靠底部状态栏展示
- 悬浮细滚动条（`MiniScrollbar`）覆盖列表右缘，不占布局
- 快捷键：Ctrl+C/X/V 复制/剪切/粘贴（系统剪贴板，跨程序互通）、Ctrl+A 全选、Delete 删除（支持多选）；粘贴重名自动 `name (2).ext`；重命名 = 慢双击选中项或右键"重命名"，行内编辑（Enter 提交/Esc 取消/失焦提交），新建后自动进入改名；Ctrl+E / 双击状态栏路径 = 在资源管理器中打开当前窗格目录
- 按住左键拖动出矩形选框框选文件（`_on_drag_1` 自实现：位移超 S(4) 才接管、Ctrl 追加、出边缘自动滚动、表头区域不接管）；选框用 GDI 直接画在 Treeview DC 上（AlphaBlend 半透明填充 + FrameRect 边框），**必须在 `after_idle` 里画**——同步绘制会被 tk 随后的选中态重绘覆盖；松开发 InvalidateRect+UpdateWindow 擦掉
- 状态文件 `qdir_lite_state.json` 放程序旁边（便携优先），不可写回退 `%APPDATA%\QDirLite\state.json`；读不到/损坏一律静默走默认

## 协作流程经验

- 每轮改动 = 小步编辑 → py_compile → 重新打包 → 进程级冒烟验证 → 必要时生成图片目检
- 用户说"先给方案"时先出方案再动手；含糊的需求（如"隐藏"的真实意图是"腾出空间"）要追问清楚再改
