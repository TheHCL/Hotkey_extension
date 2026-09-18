"""單一版本來源。release 前手動改這裡,再打相同版本號的 git tag(v<version>)。

`release.yml` 會在建置前檢查這裡的版本跟 push 的 tag 是否一致,不一致就擋下
整個 release——避免忘記改版本號,導致已安裝的 client 永遠偵測不到新版本
(更新檢查是拿這個字串跟 GitHub Release 的 tag_name 比,不是比對 zip 內容)。
"""

__version__ = "1.2.0"
