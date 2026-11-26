"""Read-only privileged LIBERO observations; never evaluates the success oracle."""

from collections import deque
import numpy as np


class LiberoSceneObserver:
    """Keep short pre-action history, isolated to one episode."""

    def __init__(self):
        self.history = deque(maxlen=5)

    def collect(self, env, step):
        domain = env.env
        sim = domain.sim
        robot = domain.robots[0]
        controller = robot.controller
        entities = {}
        models = {**domain.objects_dict, **domain.fixtures_dict}
        for name, model in models.items():
            body = domain.obj_body_id[name]
            contact_grasp = None
            if name in domain.objects_dict:
                try:
                    contact_grasp = bool(domain._check_grasp(robot.gripper, model))
                except (AttributeError, KeyError, TypeError):
                    pass
            entities[name] = {
                "position_world_m": sim.data.body_xpos[body].copy().tolist(),
                "quaternion_wxyz": sim.data.body_xquat[body].copy().tolist(),
                "both_finger_contact": contact_grasp,
                "grasp_stable": None,
                "possible_slip": None,
            }
            previous = [h["entities"].get(name, {}) for h in self.history]
            if contact_grasp is not None and len(previous) >= 2:
                prior_contact = all(p.get("both_finger_contact") is True for p in previous[-2:])
                # These are contact-based hypotheses, not ground-truth grasp/slip labels.
                entities[name]["grasp_stable"] = contact_grasp and prior_contact
                entities[name]["possible_slip"] = (
                    prior_contact and not contact_grasp
                    if self.history[-1].get("last_gripper_command", -1) > 0 else None
                )
        relative = []
        names = list(entities)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                delta = np.asarray(entities[b]["position_world_m"]) - entities[a]["position_world_m"]
                relative.append({"from": a, "to": b, "delta_world_m": delta.tolist(),
                                 "distance_m": float(np.linalg.norm(delta))})
        contacts = []
        for i in range(sim.data.ncon):
            contact = sim.data.contact[i]
            contacts.append({
                "geom1": sim.model.geom_id2name(contact.geom1),
                "geom2": sim.model.geom_id2name(contact.geom2),
                "distance_m": float(contact.dist),
            })
        limits = {}
        for name in ("input_min", "input_max", "output_min", "output_max"):
            value = getattr(controller, name, None)
            limits[name] = None if value is None else np.asarray(value).tolist()
        sample = {"step": step, "entities": entities,
                  "eef_position_world_m": np.asarray(robot.sim.data.site_xpos[robot.eef_site_id]).tolist()}
        result = {
            "source": "privileged_simulator_state",
            "entities": entities, "relative_positions": relative, "contacts": contacts,
            "desired_goal_predicates": domain.parsed_problem.get("goal_state", []),
            "target_regions": domain.parsed_problem.get("regions", {}),
            "history": list(self.history),
            "semantics": {
                "contact_is_not_necessarily_dangerous_collision": True,
                "grasp_stable": "both finger groups contacting in three consecutive samples; heuristic only",
                "possible_slip": "loss of previously sustained finger contact while previous command closes; heuristic",
                "dimensions": ["dx", "dy", "dz", "rotation_x", "rotation_y", "rotation_z", "gripper"],
                "controller": type(controller).__name__,
                "use_delta": getattr(controller, "use_delta", None),
                "controller_limits": limits,
                "action_units": "controller inputs, NOT direct meters; clipped and scaled by controller",
                "position_frame": "world",
                "gripper": "Panda normalized command: positive closes, negative opens",
                "rotation": "OSC axis-angle increment applied to current orientation",
            },
        }
        self.history.append(sample)
        return result

    def record_action(self, action):
        if self.history:
            self.history[-1]["last_gripper_command"] = float(action[-1])
