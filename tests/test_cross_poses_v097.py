from control_kernel import ControlKernel


class Output:
    enabled=True
    def __init__(self): self.pulses=[]; self.holds=[]
    def execute_action(self,action): self.pulses.append(dict(action)); return {"executed":True}
    def set_action_holds(self,holds,source_group="controls"): self.holds=list(holds); return {}
    def set_buttons(self,*a,**k): return {}
    def set_holds(self,*a,**k): return {}
    def apply(self,*a,**k): pass
    def clear_source(self,*a,**k): return {}


def pt(x,y,score=.95): return {"x":x,"y":y,"z":0.0,"score":score}


def base_pose():
    return {
      "nose":pt(.5,.3),"left_eye":pt(.47,.31),"right_eye":pt(.53,.31),"left_ear":pt(.45,.33),"right_ear":pt(.55,.33),
      "left_shoulder":pt(.40,.45),"right_shoulder":pt(.60,.45),"left_hip":pt(.44,.70),"right_hip":pt(.56,.70),
      "left_elbow":pt(.35,.55),"right_elbow":pt(.65,.55),"left_wrist":pt(.32,.60),"right_wrist":pt(.68,.60),
      "left_knee":pt(.45,.82),"right_knee":pt(.55,.82),"left_ankle":pt(.45,.95),"right_ankle":pt(.55,.95),
      "left_heel":pt(.44,.96),"right_heel":pt(.56,.96),"left_foot_index":pt(.46,.97),"right_foot_index":pt(.54,.97),
    }


def update_twice(kernel,pose):
    kernel._update_cross_poses_locked(pose,0.0); kernel._update_cross_poses_locked(pose,.03)


def test_hands_cross_is_mirror_invariant_and_debounced():
    out=Output(); k=ControlKernel(out)
    try:
        p=base_pose(); p["left_wrist"]=pt(.54,.57); p["right_wrist"]=pt(.46,.57)
        update_twice(k,p); assert "hands_cross" in k.pose_active and k.pose_confidence["hands_cross"] >= .60
        mirrored={name:{**v,"x":1-v["x"]} for name,v in p.items()}
        k.pose_debounce["hands_cross"]={"on":0,"off":0,"active":False}; update_twice(k,mirrored)
        assert "hands_cross" in k.pose_active
    finally:k.close()


def test_leg_cross_uses_leg_geometry_without_shoulders_shift_requirement():
    out=Output(); k=ControlKernel(out)
    try:
        p=base_pose(); p["right_knee"]=pt(.49,.82); p["right_ankle"]=pt(.42,.90)
        update_twice(k,p)
        assert "right_leg_cross_left" in k.pose_active
        p=base_pose(); p["left_knee"]=pt(.51,.82); p["left_ankle"]=pt(.58,.90)
        for ident in k.pose_debounce:k.pose_debounce[ident]={"on":0,"off":0,"active":False}
        update_twice(k,p); assert "left_leg_cross_right" in k.pose_active
    finally:k.close()


def test_cross_pose_dispatches_once_on_entry_not_every_frame():
    out=Output(); k=ControlKernel(out)
    try:
        k.configure_bindings({"poses":{"hands_cross":{"action":{"type":"keyboard","target":"ESC","behavior":"hold"}}}})
        k.pose_active={"hands_cross"}; k._dispatch_controls_locked(1.0); k._dispatch_controls_locked(1.03)
        assert len(out.pulses)==1
        assert out.pulses[0]["behavior"]=="tap" and out.pulses[0]["target"]=="ESC" and out.pulses[0]["nonblocking"] is True
        k.pose_active=set(); k._dispatch_controls_locked(1.06); k.pose_active={"hands_cross"}; k._dispatch_controls_locked(1.09)
        assert len(out.pulses)==2
    finally:k.close()
