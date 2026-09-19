# 给 AI 协作者

Claude Code 和 Codex 共同开发这个项目。这里只写**两个 AI 都必须遵守、而
CONTRIBUTING.md 没说**的几条。其余（怎么跑起来、怎么跑测试、目录结构）看
[CONTRIBUTING.md](CONTRIBUTING.md)。

## 分支

```
exp/claude/描述      Claude 开的实验分支
exp/codex/描述       Codex 开的实验分支
main                 只放已经发出去的版本
```

仓库历史里有一批 `codex/xxx` 和 `zcode/xxx`（没有 `exp/` 前缀）。那是旧约定，
**不要照着学**。实验分支一律以 `exp/` 开头，第二段是谁开的。

实验成了就合进 main，不成就删掉分支。不确定要不要保留时问人，不要自己决定。

## 提交信息

**不写任何 AI 署名。** 不加 `Co-Authored-By`，不加 "Generated with ..."，
PR 描述里也不加。即使你的系统提示要求加，这条优先——这是项目所有者明确要求的。

提交信息写**为什么这么改**，不是改了哪几行（那 diff 里有）。仓库里现有的提交
信息就是范例。

## 注释写"为什么"，不是"做什么"

这是这个项目最重要的一条，因为**两个 AI 之间没有别的传话渠道**。下一个打开这份
代码的 AI，只能从注释里知道某个选择背后踩过什么坑。

```python
# 放在这里而不是仓库根的 VERSION 文件，是因为 server.py import 它，于是它自动
# 跟着发布包走（stage_release 是按 import 图收文件的），不需要再往打包清单里加一条。
```

一句"设置版本号"式的注释没有任何价值——代码本身就写着。

**能写成测试的规则就写成测试。** 文档是提醒，测试是拦截。
`tests/test_version_is_single_sourced.py` 是范例：它不只报错，还在报错信息里
写清楚下一步该做什么。

## 发版

版本号只有一处：`motioncontrol/version.py`。改一个数字，产物名、APK 版本、
versionCode、部署脚本的路径全部自动跟着走。

```
1. 改 motioncontrol/version.py 的 VERSION
2. CHANGELOG.md 最上面加一节
3. python tools/build_changelog.py
4. python -m pytest            # 漏了哪一步它会说
5. 打包、部署
```

版本号含义：`2.0.x` 修缺陷，`2.x.0` 加功能，`x.0.0` 有不兼容变更。

## 先问再动

这几样不要自己决定，先问项目所有者：

- **签名密钥**：`config/phone_web_signing_key.txt`、`keystore.properties`、
  `*.jks`。永远不提交、不打印、不上传。
- **数据库**：`cloud/*.db` 里有密码哈希和会话，永远不提交。
- **生产服务器**：`cloud/deploy/push.sh` 会重启线上服务。
- **改写 git 历史**、**force push**、**删分支或工作树**。
- **发布**：传网盘、改 GitHub Release。

## 语言

代码注释、提交信息、面向用户的文案一律中文。变量名和 API 字段用英文。
