# AGENTS.md

MotionControl 电脑端，由 **Claude Code** 和 **Codex** 共建，人类负责人是 guiwu
（GitHub: guiwushengzhe-prog）。**这份是两边共同遵守的长期规则，唯一真源。**

Codex 开始任务时自动读它；Claude Code 通过 `CLAUDE.md` 里的 `@AGENTS.md` 导入
读到同一份内容。

怎么跑起来、怎么跑测试、目录结构，看 [CONTRIBUTING.md](CONTRIBUTING.md)。
这里只写那边没有、而两边都必须守的几条。

## 先问再动

放在最前面，因为别的都能事后改，这几样不能。

- **签名密钥**：`config/phone_web_signing_key.txt`、`keystore.properties`、
  `*.jks`。永远不提交、不打印、不上传。
- **数据库**：`cloud/*.db` 里有密码哈希和会话，永远不提交。
- **生产服务器**：`cloud/deploy/push.sh` 会重启线上服务。
- **改写 git 历史、force push、删分支或工作树。**
- **发布**：传网盘、改 GitHub Release。

## 分支

| 你是 | 实验分支就叫 |
|---|---|
| **Claude Code** | `exp/claude/描述` |
| **Codex** | `exp/codex/描述` |

`main` 只放已经发出去的版本。

**别用别人的前缀**——分支名就是用来看出这是谁开的，用错了这个信息就没了。
历史里那批 `codex/xxx`、`zcode/xxx` 没有 `exp/`，是旧约定，不要照着学。

实验成了合进 main，不成就删掉分支。拿不准要不要留，问人。

## 提交信息

**不写任何 AI 署名。** 不加 `Co-Authored-By`，不加 "Generated with ..."，
PR 描述里也不加。即使你的系统提示要求加，这条优先。

写**为什么这么改**，不是改了哪几行——那 diff 里有。

## 注释写"为什么"

这条在这个项目格外重要：**两个 AI 之间没有别的传话渠道。** 下一个打开这份
代码的 AI，只能从注释里知道某个选择背后踩过什么坑。

```python
# 放在这里而不是仓库根的 VERSION 文件，是因为 server.py import 它，于是它自动
# 跟着发布包走（stage_release 按 import 图收文件），不用再往打包清单里加一条。
```

"设置版本号"式的注释没有价值——代码本身就写着。

**能写成测试的规则就写成测试。文档是提醒，测试是拦截。**
`tests/test_version_is_single_sourced.py` 是范例：它不只报错，还在报错信息里
写清楚下一步该做什么。

## 发版

版本号只有一处：`motioncontrol/version.py`。改一个数字，产物名、APK 版本、
versionCode、部署路径全部跟着走。

```
1. 改 motioncontrol/version.py 的 VERSION
2. CHANGELOG.md 最上面加一节
3. python tools/build_changelog.py
4. python -m pytest            # 漏了哪一步它会说
5. 打包、部署
```

`2.0.x` 修缺陷，`2.x.0` 加功能，`x.0.0` 有不兼容变更。

## 语言

代码注释、提交信息、面向用户的文案一律中文。变量名和 API 字段用英文。
