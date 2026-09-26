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
    $speechAssembly = [System.Speech.Recognition.SpeechRecognitionEngine].Assembly.Location
    Add-Type -ReferencedAssemblies $speechAssembly -TypeDefinition @"
using System;
using System.Globalization;
using System.IO;
using System.Speech.Recognition;
using System.Threading;

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
        // PowerShell's redirected stdin can report a short read followed by
        // zero while the writer is between 100 ms PCM frames.  System.Speech
        // treats that transient zero as end-of-stream and exits.  Keep the
        // stream alive until the parent closes it.
        int total = 0;
        while (total < count)
        {
            int read = inner.Read(buffer, offset + total, count - total);
            if (read > 0)
            {
                total += read;
                position += read;
                continue;
            }
            Thread.Sleep(10);
        }
        return total;
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

// PowerShell script-block event handlers run on SpeechRecognitionEngine's
// worker thread.  PowerShell 5.1 can throw ScriptBlock.GetContextFromTLS on
// that thread and silently kill the recognizer.  Keep the callbacks in CLR
// code so recognition events are independent of the PowerShell runspace.
public sealed class MotionControlSpeechSink : IDisposable
{
    private readonly SpeechRecognitionEngine engine;
    private readonly ManualResetEventSlim completed;

    public MotionControlSpeechSink(SpeechRecognitionEngine speechEngine, ManualResetEventSlim done)
    {
        engine = speechEngine;
        completed = done;
        engine.SpeechRecognized += OnRecognized;
        engine.SpeechHypothesized += OnHypothesized;
        engine.RecognizeCompleted += OnCompleted;
    }

    private static string Escape(string value)
    {
        return (value ?? string.Empty)
            .Replace("\\", "\\\\")
            .Replace("\"", "\\\"")
            .Replace("\r", "\\r")
            .Replace("\n", "\\n");
    }

    private static void WriteEvent(string kind, string text, double? confidence = null)
    {
        string json = "{\"kind\":\"" + Escape(kind) + "\",\"text\":\"" + Escape(text) + "\"";
        if (confidence.HasValue)
            json += ",\"confidence\":" + confidence.Value.ToString("R", CultureInfo.InvariantCulture);
        Console.WriteLine(json + "}");
        Console.Out.Flush();
    }

    private static void WriteError(string message)
    {
        Console.WriteLine("{\"kind\":\"error\",\"message\":\"" + Escape(message) + "\"}");
        Console.Out.Flush();
    }

    private void OnRecognized(object sender, SpeechRecognizedEventArgs args)
    {
        WriteEvent("final", args.Result == null ? string.Empty : args.Result.Text,
            args.Result == null ? (double?)null : args.Result.Confidence);
    }

    private void OnHypothesized(object sender, SpeechHypothesizedEventArgs args)
    {
        WriteEvent("partial", args.Result == null ? string.Empty : args.Result.Text);
    }

    private void OnCompleted(object sender, RecognizeCompletedEventArgs args)
    {
        if (args.Error != null)
            WriteError(args.Error.Message);
        completed.Set();
    }

    public void Dispose()
    {
        engine.SpeechRecognized -= OnRecognized;
        engine.SpeechHypothesized -= OnHypothesized;
        engine.RecognizeCompleted -= OnCompleted;
    }
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
    $sink = New-Object -TypeName MotionControlSpeechSink -ArgumentList $engine, $completed

    Send-Event @{ kind = "ready"; culture = $recognizerInfo.Culture.Name }
    $engine.RecognizeAsync([System.Speech.Recognition.RecognizeMode]::Multiple)
    $completed.Wait()
    $sink.Dispose()
    $engine.Dispose()
    $audioStream.Dispose()
}
catch {
    Send-Event @{ kind = "error"; message = [string]$_.Exception.Message }
    exit 1
}
