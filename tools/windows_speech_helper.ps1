param(
    [Parameter(Mandatory = $true)]
    [string]$PhrasesBase64,
    [int]$SampleRate = 16000
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)

function Send-Event([hashtable]$Event) {
    [Console]::WriteLine(($Event | ConvertTo-Json -Compress -Depth 4))
    [Console]::Out.Flush()
}

try {
    Add-Type -AssemblyName System.Speech
    Add-Type -TypeDefinition @"
using System;
using System.IO;

public sealed class MotionControlPcmStream : Stream
{
    private readonly Stream inner;
    private long position;

    public MotionControlPcmStream(Stream innerStream) { inner = innerStream; }
    public override bool CanRead { get { return true; } }
    public override bool CanSeek { get { return true; } }
    public override bool CanWrite { get { return false; } }
    public override long Length { get { return long.MaxValue; } }
    public override long Position { get { return position; } set { position = value; } }
    public override int Read(byte[] buffer, int offset, int count)
    {
        int read = inner.Read(buffer, offset, count);
        if (read > 0) position += read;
        return read;
    }
    public override long Seek(long offset, SeekOrigin origin)
    {
        if (origin == SeekOrigin.Begin) position = offset;
        else if (origin == SeekOrigin.Current) position += offset;
        else position = long.MaxValue;
        return position;
    }
    public override void Flush() { }
    public override void SetLength(long value) { throw new NotSupportedException(); }
    public override void Write(byte[] buffer, int offset, int count) { throw new NotSupportedException(); }
}
"@
    $json = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($PhrasesBase64))
    $phrases = @($json | ConvertFrom-Json)
    if ($phrases.Count -eq 0) {
        throw "没有可用的语音口令"
    }

    $recognizerInfo = [System.Speech.Recognition.SpeechRecognitionEngine]::InstalledRecognizers() |
        Where-Object { $_.Culture.Name -eq "zh-CN" } |
        Select-Object -First 1
    if ($null -eq $recognizerInfo) {
        throw "Windows 未安装中文语音识别组件（zh-CN）"
    }

    $engine = New-Object -TypeName System.Speech.Recognition.SpeechRecognitionEngine -ArgumentList $recognizerInfo.Culture
    $choices = New-Object System.Speech.Recognition.Choices
    foreach ($phrase in $phrases) {
        $value = ([string]$phrase).Trim()
        if ($value) {
            [void]$choices.Add($value)
        }
    }
    $builder = New-Object -TypeName System.Speech.Recognition.GrammarBuilder
    # GrammarBuilder 默认使用当前 PowerShell 语言（可能是 en-US），
    # 但这里加载的是中文识别器；语言不一致时 LoadGrammar 会直接失败。
    $builder.Culture = $recognizerInfo.Culture
    [void]$builder.Append($choices)
    $grammar = New-Object -TypeName System.Speech.Recognition.Grammar -ArgumentList $builder
    $grammar.Name = "MotionControl"
    $engine.LoadGrammar($grammar)

    $format = New-Object -TypeName System.Speech.AudioFormat.SpeechAudioFormatInfo -ArgumentList @(
        $SampleRate,
        [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
        [System.Speech.AudioFormat.AudioChannel]::Mono
    )
    $audioStream = New-Object -TypeName MotionControlPcmStream -ArgumentList ([Console]::OpenStandardInput())
    $engine.SetInputToAudioStream($audioStream, $format)
    $completed = New-Object System.Threading.ManualResetEventSlim($false)
    $engine.add_SpeechRecognized({
        param($sender, $event)
        Send-Event @{ kind = "final"; text = [string]$event.Result.Text; confidence = [double]$event.Result.Confidence }
    })
    $engine.add_SpeechHypothesized({
        param($sender, $event)
        Send-Event @{ kind = "partial"; text = [string]$event.Result.Text }
    })
    $engine.add_RecognizeCompleted({
        param($sender, $event)
        if ($null -ne $event.Error) {
            Send-Event @{ kind = "error"; message = [string]$event.Error.Message }
        }
        $completed.Set()
    })

    Send-Event @{ kind = "ready"; culture = $recognizerInfo.Culture.Name }
    $engine.RecognizeAsync([System.Speech.Recognition.RecognizeMode]::Multiple)
    $completed.Wait()
    $engine.Dispose()
    $audioStream.Dispose()
}
catch {
    Send-Event @{ kind = "error"; message = [string]$_.Exception.Message }
    exit 1
}
