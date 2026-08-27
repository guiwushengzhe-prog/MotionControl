$ErrorActionPreference = 'Stop'

# Windows SAPI is the preferred corpus generator.  On this host it enumerates
# Chinese voices but only writes a 46-byte empty WAV, so this isolated pressure
# corpus uses two Microsoft Edge Chinese voices as a generation fallback.  The
# runtime candidates remain fully offline; no audio is sent to the product.
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$corpusDir = Join-Path $root 'corpus'
New-Item -ItemType Directory -Force -Path $corpusDir | Out-Null
$edge = (Get-Command 'edge-tts.exe' -ErrorAction SilentlyContinue).Source
if (-not $edge) { $edge = 'C:\Users\Lenovo\AppData\Local\hermes\hermes-agent\venv\Scripts\edge-tts.exe' }
$ffmpeg = (Get-Command 'ffmpeg.exe' -ErrorAction SilentlyContinue).Source
if (-not $ffmpeg) { throw 'ffmpeg.exe is required for PCM conversion' }
if (-not (Test-Path -LiteralPath $edge)) { throw "edge-tts not found: $edge" }

$voices = @(
    [ordered]@{ id = 'xiaoxiao'; name = 'zh-CN-XiaoxiaoNeural' },
    [ordered]@{ id = 'yunxi'; name = 'zh-CN-YunxiNeural' }
)
$items = @(
    [ordered]@{ id = 'head_calibration'; text = '体感开始校准'; expected_intent = 'head.calibrate'; kind = 'positive'; stress = $true; mapping = 'generated_voice/voice_action_map.json' },
    [ordered]@{ id = 'emergency_stop'; text = '体感紧急停止'; expected_intent = 'system.emergency_stop'; kind = 'positive'; stress = $true; emergency = $true; mapping = 'generated_voice/voice_action_map.json' },
    [ordered]@{ id = 'nav_up'; text = '体感菜单上'; expected_intent = 'nav.up'; kind = 'positive'; stress = $true; mapping = 'generated_voice/voice_action_map.json' },
    [ordered]@{ id = 'nav_down'; text = '体感菜单下'; expected_intent = 'nav.down'; kind = 'positive'; stress = $true; mapping = 'generated_voice/voice_action_map.json' },
    [ordered]@{ id = 'nav_left'; text = '体感菜单左'; expected_intent = 'nav.left'; kind = 'positive'; stress = $true; mapping = 'generated_voice/voice_action_map.json' },
    [ordered]@{ id = 'nav_right'; text = '体感菜单右'; expected_intent = 'nav.right'; kind = 'positive'; stress = $true; mapping = 'generated_voice/voice_action_map.json' },
    [ordered]@{ id = 'nav_up_synonym'; text = '体感向上'; expected_intent = 'nav.up'; kind = 'positive'; stress = $true; mapping = 'config/voice_mappings.json synonym' },
    [ordered]@{ id = 'nav_left_synonym'; text = '体感向左'; expected_intent = 'nav.left'; kind = 'positive'; stress = $true; mapping = 'config/voice_mappings.json synonym' },
    [ordered]@{ id = 'nav_right_synonym'; text = '体感向右'; expected_intent = 'nav.right'; kind = 'positive'; stress = $true; mapping = 'config/voice_mappings.json synonym' },
    [ordered]@{ id = 'output_start'; text = '体感开始输出'; expected_intent = 'output.start'; kind = 'positive'; stress = $true; mapping = 'generated_voice/voice_action_map.json' },
    [ordered]@{ id = 'output_stop'; text = '体感停止输出'; expected_intent = 'output.stop'; kind = 'positive'; stress = $true; mapping = 'generated_voice/voice_action_map.json' },
    [ordered]@{ id = 'accelerate'; text = '体感加速'; expected_intent = 'game.accelerate'; kind = 'positive'; stress = $true; mapping = 'generated_voice/voice_action_map.json' },
    [ordered]@{ id = 'attack'; text = '体感攻击'; expected_intent = 'game.attack'; kind = 'positive'; stress = $true; mapping = 'generated_voice/voice_action_map.json' },
    [ordered]@{ id = 'dodge'; text = '体感闪避'; expected_intent = 'game.dodge'; kind = 'positive'; stress = $true; mapping = 'generated_voice/voice_action_map.json' },
    [ordered]@{ id = 'jump'; text = '体感跳跃'; expected_intent = 'game.jump'; kind = 'positive'; stress = $false; mapping = 'generated_voice/voice_action_map.json' },
    [ordered]@{ id = 'reload'; text = '体感换弹'; expected_intent = 'game.reload'; kind = 'positive'; stress = $false; mapping = 'generated_voice/voice_action_map.json' },
    [ordered]@{ id = 'skill'; text = '体感技能'; expected_intent = 'game.skill'; kind = 'positive'; stress = $false; mapping = 'generated_voice/voice_action_map.json' },
    [ordered]@{ id = 'map'; text = '体感地图'; expected_intent = 'ui.map'; kind = 'positive'; stress = $false; mapping = 'generated_voice/voice_action_map.json' },
    [ordered]@{ id = 'screenshot'; text = '体感截图'; expected_intent = 'scene.capture'; kind = 'positive'; stress = $false; mapping = 'generated_voice/voice_action_map.json' },
    [ordered]@{ id = 'center'; text = '体感设置中心'; expected_intent = 'head.center'; kind = 'positive'; stress = $false; mapping = 'generated_voice/voice_action_map.json' },
    [ordered]@{ id = 'calibration_synonym'; text = '体感自动校准'; expected_intent = 'head.calibrate'; kind = 'positive'; stress = $true; mapping = 'config/voice_mappings.json synonym' },
    [ordered]@{ id = 'wake_only'; text = '体感'; expected_intent = $null; kind = 'negative'; stress = $true; mapping = 'wake only' },
    [ordered]@{ id = 'near_emergency'; text = '体感紧急制止'; expected_intent = $null; kind = 'negative'; stress = $true; mapping = 'near miss' },
    [ordered]@{ id = 'near_calibration'; text = '体感开始校准吗'; expected_intent = $null; kind = 'negative'; stress = $true; mapping = 'near miss' },
    [ordered]@{ id = 'homophone_left'; text = '体感向量'; expected_intent = $null; kind = 'negative'; stress = $true; mapping = 'homophone-like' },
    [ordered]@{ id = 'near_left'; text = '体感左边'; expected_intent = $null; kind = 'negative'; stress = $true; mapping = 'near miss' },
    [ordered]@{ id = 'question_menu'; text = '体感菜单上了吗'; expected_intent = $null; kind = 'negative'; stress = $true; mapping = 'near miss' },
    [ordered]@{ id = 'no_wake_calibration'; text = '开始校准'; expected_intent = $null; kind = 'negative'; stress = $true; mapping = 'missing wake word' },
    [ordered]@{ id = 'ordinary_weather'; text = '今天天气不错'; expected_intent = $null; kind = 'negative'; stress = $true; mapping = 'ordinary sentence' },
    [ordered]@{ id = 'ordinary_light'; text = '请把灯打开'; expected_intent = $null; kind = 'negative'; stress = $true; mapping = 'ordinary sentence' },
    [ordered]@{ id = 'ordinary_rest'; text = '我想休息一下'; expected_intent = $null; kind = 'negative'; stress = $false; mapping = 'ordinary sentence' },
    [ordered]@{ id = 'ordinary_button'; text = '这个按钮在哪里'; expected_intent = $null; kind = 'negative'; stress = $false; mapping = 'ordinary sentence' },
    [ordered]@{ id = 'near_start'; text = '体感开始了吗'; expected_intent = $null; kind = 'negative'; stress = $true; mapping = 'near miss' },
    [ordered]@{ id = 'no_wake_start'; text = '启动一下'; expected_intent = $null; kind = 'negative'; stress = $false; mapping = 'missing wake word' }
)

$written = @()
foreach ($voice in $voices) {
    foreach ($item in $items) {
        $stem = "{0}__{1}" -f $item.id, $voice.id
        $mp3 = Join-Path $corpusDir "$stem.mp3"
        $wav = Join-Path $corpusDir "$stem.wav"
        if (-not (Test-Path -LiteralPath $wav) -or (Get-Item -LiteralPath $wav).Length -le 1000) {
            Remove-Item -LiteralPath $mp3,$wav -Force -ErrorAction SilentlyContinue
            $ok = $false
            for ($attempt = 1; $attempt -le 3 -and -not $ok; $attempt++) {
                & $edge --voice $voice.name --text $item.text --write-media $mp3 | Out-Null
                if ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $mp3)) {
                    & $ffmpeg -y -loglevel error -i $mp3 -ac 1 -ar 16000 -sample_fmt s16 $wav
                    $ok = ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $wav) -and (Get-Item -LiteralPath $wav).Length -gt 1000)
                }
                if (-not $ok -and $attempt -lt 3) { Start-Sleep -Seconds 1 }
            }
            if (-not $ok) { throw "edge-tts/ffmpeg failed for $($item.text) / $($voice.name) after 3 attempts" }
            Remove-Item -LiteralPath $mp3 -Force -ErrorAction SilentlyContinue
        }
        $written += [ordered]@{
            id = $item.id
            voice_id = $voice.id
            voice_name = $voice.name
            text = $item.text
            expected_intent = $item.expected_intent
            kind = $item.kind
            stress = [bool]$item.stress
            emergency = [bool]($item.emergency -eq $true)
            mapping = $item.mapping
            file = "corpus/$stem.wav"
        }
    }
}

[ordered]@{
    schema = 'voice_robustness_corpus_v1'
    generated_by = 'Microsoft Edge TTS fallback (isolated corpus only)'
    generator_note = 'Windows SAPI Chinese voices enumerated but produced 46-byte empty WAVs in this non-interactive host; Edge TTS was used only to create pressure-proxy audio. Runtime recognition remains offline.'
    sample_rate = 16000
    voices = $voices
    items = $written
} | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $root 'tts_manifest.json') -Encoding UTF8
Write-Output ("Generated {0} speech clips with {1} Edge Chinese voices under {2}" -f $written.Count, $voices.Count, $corpusDir)
