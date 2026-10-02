"""Visible-state rendering regressions using the real browser modules.

API replies are isolated; assertions measure DOM mutations, canvas work, preserved
nodes and actual clicks rather than matching JavaScript implementation text.
"""
import copy

from test_web_optimization import desktop, mock_browser  # noqa: F401


MEASURE = """async task=>{
  const records=[];const observer=new MutationObserver(items=>records.push(...items));
  observer.observe(document.body,{subtree:true,childList:true,attributes:true,characterData:true});
  let draws=0;const original=CanvasRenderingContext2D.prototype.clearRect;
  CanvasRenderingContext2D.prototype.clearRect=function(...args){draws++;return original.apply(this,args)};
  try{await task();records.push(...observer.takeRecords());return {records:records.length,childList:records.filter(r=>r.type==='childList').length,draws}}
  finally{observer.disconnect();CanvasRenderingContext2D.prototype.clearRect=original}
}"""


def runtime_with_pose(api):
    runtime = copy.deepcopy(api.runtime)
    runtime["kernel"].update(pose={"nose": {"x": .5, "y": .3, "score": .9}},
                             zones={"leftHand": {"pressed": False, "phase": "idle",
                                    "rect": {"x1": .123456789, "x2": .34567891, "y1": .1, "y2": .3}}})
    return runtime


def test_visible_poll_does_not_rewrite_static_fields_or_redraw_identical_pose(desktop):
    runtime = runtime_with_pose(desktop.api)
    result = desktop.page.evaluate("""async runtime=>{
      const play=await import('/js/play.js');play.renderKernelState(runtime);play.renderKernelState(runtime);
      const measure=""" + MEASURE + """;
      return measure(()=>{for(let i=0;i<20;i++){const next=structuredClone(runtime);next.kernel.now+=i/4;play.renderKernelState(next)}});
    }""", runtime)
    assert result["draws"] == 0
    assert result["childList"] == 0
    # A broad browser mutation count is diagnostic, not a claimed FPS metric.
    assert result["records"] < 10


def test_pose_changes_and_zone_editor_remain_independent_of_draw_skipping(desktop):
    api, page = desktop.api, desktop.page
    api.runtime = runtime_with_pose(api)
    api.runtime["kernel"]["zones_frozen"] = True
    result = page.evaluate("""async runtime=>{
      const play=await import('/js/play.js');play.renderKernelState(runtime);
      let draws=0;const original=CanvasRenderingContext2D.prototype.clearRect;
      CanvasRenderingContext2D.prototype.clearRect=function(...args){draws++;return original.apply(this,args)};
      try{
        play.renderKernelState(structuredClone(runtime));const unchanged=draws;
        runtime.kernel.pose.nose.x=.6;play.renderKernelState(runtime);const moved=draws;
        runtime.kernel.zones.leftHand.rect.x1=.15;runtime.kernel.zones.leftHand.phase='pending';play.renderKernelState(runtime);
        const zone=document.querySelector('.zone[data-zone="leftHand"]');const zoneUpdated=zone.style.left==='15%'&&zone.classList.contains('pending');
        await play.openLiveZoneEditor();const before=zone.style.left;play.nudgeRect('leftHand','ArrowRight',false);
        const editorUpdated=zone.style.left!==before&&zone.classList.contains('selected');
        runtime.kernel.pose.nose.score=.2;play.renderKernelState(runtime);const disappeared=draws;
        runtime.kernel.pose.nose.x=.7;play.renderKernelState(runtime);const stillInvisible=draws;
        runtime.kernel.width=1000;play.renderKernelState(runtime);const resized=draws;
        return {unchanged,moved,zoneUpdated,editorUpdated,disappeared,stillInvisible,resized};
      }finally{CanvasRenderingContext2D.prototype.clearRect=original}
    }""", api.runtime)
    assert result["unchanged"] == 0
    assert result["moved"] == 1
    assert result["zoneUpdated"] is True
    assert result["editorUpdated"] is True
    assert result["disappeared"] > result["moved"]
    assert result["stillInvisible"] == result["disappeared"]
    assert result["resized"] > result["stillInvisible"]


def test_a_recent_trigger_jumps_to_its_row(desktop):
    page, api = desktop.page, desktop.api
    api.runtime["kernel"]["recent_triggers"] = [{"at": 99, "trigger": "zone.leftHand", "label": "左手区",
                                                  "action": {"type": "gamepad", "target": "X"}}]
    page.evaluate("async runtime=>(await import('/js/play.js')).renderKernelState(runtime)", api.runtime)
    page.click("#rangeLog button")
    row = page.locator('.binding-row[data-trigger="zone.leftHand"]')
    assert row.is_visible()
    assert "just-found" in row.get_attribute("class")
    assert page.evaluate("document.activeElement.closest('.binding-row')?.dataset.trigger") == "zone.leftHand"
    assert page.locator('[data-view="games"]').get_attribute("aria-current") == "page"
    assert page.locator("#triggerLive").count() == 0


def test_calibration_countdown_updates_without_redrawing_the_same_pose(desktop):
    runtime = runtime_with_pose(desktop.api)
    runtime["kernel"]["head"].update(calibrating=True, center_phase="prepare", center_remaining_s=2,
                                       notice="look at center")
    result = desktop.page.evaluate("""async runtime=>{
      const play=await import('/js/play.js');play.renderKernelState(runtime);
      const layer=calibrationOverlay,cancel=calibrationCancel;cancel.focus();
      const measure=""" + MEASURE + """;
      const measured=await measure(()=>{
        runtime.kernel.head.center_remaining_s=1.2;runtime.kernel.head.center_phase='collect';
        runtime.kernel.head.center_valid_s=.5;runtime.kernel.head.center_sample_count=4;
        runtime.kernel.head.notice='keep still';play.renderKernelState(runtime);
      });
      return {draws:measured.draws,countdown:calibrationCountdown.textContent,prompt:calibrationPrompt.textContent,detail:calibrationDetail.textContent,open:layer.open,focused:document.activeElement===cancel};
    }""", runtime)
    assert result["draws"] == 0
    assert result["countdown"] == "1.2"
    assert result["prompt"] == "keep still"
    assert "4 /" in result["detail"]
    assert result["open"] is True
    assert result["focused"] is True


def test_recent_events_keep_buttons_and_focus_when_age_and_log_order_change(desktop):
    runtime = copy.deepcopy(desktop.api.runtime)
    runtime["kernel"]["recent_triggers"] = [{"at": 99, "trigger": "zone.leftHand", "label": "左手区",
                                             "action": {"type": "gamepad", "target": "X"}}]
    result = desktop.page.evaluate("""async runtime=>{
      const play=await import('/js/play.js');play.renderKernelState(runtime);
      const row=rangeLog.firstElementChild;row.focus();const oldText=row.textContent;
      runtime.kernel.now=101.25;play.renderKernelState(runtime);
      const ageChanged=row.textContent!==oldText,ageFocus=document.activeElement===row;
      runtime.kernel.recent_triggers.push({at:101.2,trigger:'zone.rightHand',label:'右手区',action:{type:'gamepad',target:'Y'}});
      play.renderKernelState(runtime);
      return {ageChanged,ageFocus,nodeStable:rangeLog.lastElementChild===row,newEvent:rangeLog.firstElementChild.textContent.includes('右手区'),focusStable:document.activeElement===row};
    }""", runtime)
    assert all(result.values())


def test_voice_status_keeps_draft_and_command_cards_update_in_place(desktop):
    page, api = desktop.page, desktop.api
    api.commands = [{"id": "game.profile_slot_1", "phrase": "jump", "effective_action": {"type": "keyboard", "target": "W"}},
                    {"id": "game.profile_slot_2", "phrase": "map", "effective_action": {"type": "keyboard", "target": "M"}}]
    page.click('[data-view="devices"]')
    page.click('#settingsNav [data-pane="voice"]')
    phrase = page.locator("#voiceRows .voice-phrase").first
    phrase.fill("unsaved phrase")
    result = page.evaluate("""async()=>{
      const voice=await import('/js/voice.js');await voice.refreshVoice();await voice.refreshVoiceCommands();
      window.firstCommand=voiceCommandGrid.querySelector('.voice-command-card');window.secondCommand=firstCommand.nextElementSibling;
      const focus=document.activeElement,value=focus.value,records=[];
      const observer=new MutationObserver(items=>records.push(...items));observer.observe(voiceCommandGrid,{subtree:true,childList:true,attributes:true});
      for(let i=0;i<20;i++){await voice.refreshVoice();await voice.refreshVoiceCommands()}
      records.push(...observer.takeRecords());observer.disconnect();
      return {mutations:records.length,focused:document.activeElement===focus,draft:focus.value===value,firstStable:voiceCommandGrid.querySelector('.voice-command-card')===firstCommand};
    }""")
    assert result == {"mutations": 0, "focused": True, "draft": True, "firstStable": True}
    api.commands[0]["effective_action"]["target"] = "Y"
    page.evaluate("import('/js/voice.js').then(m=>m.refreshVoiceCommands())")
    assert page.evaluate("voiceCommandGrid.querySelector('.voice-command-card')===firstCommand && firstCommand.nextElementSibling===secondCommand")
    assert "Y" in page.locator("#voiceCommandGrid .voice-command-card").first.inner_text()
    assert phrase.input_value() == "unsaved phrase"


def test_release_choices_keep_existing_nodes_for_equal_and_changed_labels(desktop):
    page, api = desktop.page, desktop.api
    hold = {"type": "keyboard", "target": "W", "behavior": "hold"}
    release = {"type": "voice_release", "target": ["game.profile_slot_1"]}
    api.commands = [{"id": "game.profile_slot_1", "phrase": "hold", "default_action": hold, "effective_action": hold},
                    {"id": "game.profile_slot_2", "phrase": "release", "default_action": release, "effective_action": release}]
    api.profile["bindings"]["voice"].update({"game.profile_slot_1": {"phrase": "hold", "action": hold},
                                             "game.profile_slot_2": {"phrase": "release", "action": release}})
    page.reload()
    page.wait_for_function("currentGameName.textContent==='Default game'")
    page.click('[data-view="games"]')
    page.click('#mapTabs [data-tab="voice"]')
    page.click('.binding-row[data-trigger="voice.game.profile_slot_2"] .voice-release-picker-button')
    result = page.evaluate("""async()=>{
      const mapping=await import('/js/mapping.js');const picker=document.querySelector('.voice-release-picker.open');
      const option=picker.querySelector('.voice-release-option');option.focus();const selected=picker.querySelector('select').selectedOptions[0];
      const records=[];const observer=new MutationObserver(items=>records.push(...items));observer.observe(picker,{subtree:true,childList:true,attributes:true});
      for(let i=0;i<20;i++)mapping.syncVoiceReleaseChoices();records.push(...observer.takeRecords());observer.disconnect();
      const unchanged=records.length;
      document.querySelector('.binding-row[data-trigger="voice.game.profile_slot_1"] .voice-trigger-phrase').value='new hold';mapping.syncVoiceReleaseChoices();
      return {unchanged,optionStable:picker.querySelector('.voice-release-option')===option,selectStable:picker.querySelector('select').selectedOptions[0]===selected,labelUpdated:option.textContent.includes('new hold'),focused:document.activeElement===option};
    }""")
    assert result == {"unchanged": 0, "optionStable": True, "selectStable": True, "labelUpdated": True, "focused": True}
