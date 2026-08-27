$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$corpusDir = Join-Path $root 'corpus'
New-Item -ItemType Directory -Force -Path $corpusDir | Out-Null

Add-Type -AssemblyName System.Speech

$wantedVoices = @(
    [ordered]@{ id = 'huihui_desktop'; name = 'Microsoft Huihui Desktop' },
    [ordered]@{ id = 'kangkang'; name = 'Microsoft Kangkang' }
)

$installed = @(
    (New-Object System.Speech.Synthesis.SpeechSynthesizer).GetInstalledVoices() |
        ForEach-Object { $_.VoiceInfo.Name }
)
$voices = @($wantedVoices | Where-Object { $installed -contains $_.name })
if ($voices.Count -eq 0) {
    throw "No requested Chinese SAPI voice is installed. Installed voices: $($installed -join ', ')"
}

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

$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.Rate = 0
$synth.Volume = 100
$written = @()
try {
    foreach ($voice in $voices) {
        $synth.SelectVoice($voice.name)
        foreach ($item in $items) {
            $fileName = "{0}__{1}.wav" -f $item.id, $voice.id
            $path = Join-Path $corpusDir $fileName
            $synth.SetOutputToWaveFile($path)
            $synth.Speak($item.text)
            $synth.SetOutputToNull()
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
                file = "corpus/$fileName"
            }
        }
    }
}
finally {
    $synth.Dispose()
}

$manifest = [ordered]@{
    schema = 'voice_robustness_corpus_v1'
    generated_by = 'Windows System.Speech SAPI'
    sample_rate_source = 'SAPI default; runner resamples to 16000 Hz mono'
    voices_requested = $wantedVoices
    voices_available = $voices
    items = $written
}
$manifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $root 'tts_manifest.json') -Encoding UTF8
Write-Output ("Generated {0} speech clips with {1} Chinese SAPI voices under {2}" -f $written.Count, $voices.Count, $corpusDir)
