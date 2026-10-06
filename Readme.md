# AutoJs-Sublime-Plugin

桌面编辑器Sublime Text 3的插件。可以让Sublime Text 3支持[AutoJs](https://github.com/hyb1996/NoRootScriptDroid)开发。目前功能比较基础，仅支持：
* 在Sublime的控制台实时显示AutoJs的日志与输出
* 在Sublime的菜单栏中增加Run, Stop, Rerun, Stop all等选项。可以在手机与电脑连接后把Sublime编辑器中的脚本推送到AutoJs中执行，或者停止AutoJs中运行的脚本。


## How to use

### Step 1
Sublime菜单Preferences(首选项)->Browse Package(浏览插件)。这将会打开一个文件夹。进入该文件夹的上一级，找到Installed Packages文件夹。将[autojs.sublime-package](https://raw.githubusercontent.com/hyb1996/AutoJs-Sublime-Plugin/master/autojs.sublime-package)复制到里面。重启Sublime,在菜单栏中看到AutoJs选项表示安装成功。

### Step 2
菜单栏中AutoJs->Start Server启动服务。通过Ctrl+`或者菜单View->Show Console显示控制台。在控制台中出现waiting for accepting表示启用成功。

### Step 3
将手机连接到电脑启用的Wifi或者同一局域网中。通过命令行ipconfig(或者其他操作系统的相同功能命令)查看电脑的IP地址。在[AutoJs](https://github.com/hyb1996/NoRootScriptDroid)的侧拉菜单中启用调试服务，并输入IP地址。显示Connected表示连接成功。

新版 AutoX.js 使用 WebSocket 协议(端口 9317)，在"连接电脑"中填入`电脑IP:9317`即可；旧版 Auto.js 连接 1209 端口。

### Step 4
然后就可以通过在Sublime Text打代码，在手机运行啦~

可以借助DDMS来抓取布局信息。

## Commands

* Run 运行脚本
* Rerun 停止当前文件对应的脚本并重新运行
* Stop 停止当前文件对应的脚本
* StopAll 停止所有正在运行的脚本
* Save 保存当前文件到手机的脚本默认目录。（文件名会加上<remote>前缀)
