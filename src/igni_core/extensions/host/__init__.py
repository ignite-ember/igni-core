"""The host object model: igni, as an extension sees it.

``docs/HOST_OBJECT_MODEL.md`` is the model; ``docs/HOST_MODEL_PROTOCOL.md``
its wire. Here:

* :mod:`.spec` — the grammar the model is written in;
* :mod:`.definition` — the model itself, versioned: every object,
  property, signal and method, and the permission each needs;
* :mod:`.coverage` — where every RPC, UI action and broadcast lives in it;
* :mod:`.codegen` — both SDKs, generated from the definition;
* :mod:`.model` — the model served: :meth:`HostModel.build
  <.model.HostModel.build>` composes it from a :mod:`.model_config`,
  and :meth:`HostModel.handle <.model.HostModel.handle>` answers one
  request from one :mod:`.caller` — from both channels, the process's
  loopback socket (:mod:`.channel`) and the page's bridge
  (:mod:`.page_channel`). Each request is checked against the
  extension's grants and the schema (:mod:`.request_binder`) and run by
  the member that implements it (:mod:`.members`, each against igni's
  backend; :mod:`.member_table`). The schema is the authority: a member
  it does not define does not exist; a stage-2 member answers that it
  is not yet available; a member not granted answers ``not_granted``
  and its signals never arrive;
* :mod:`.broadcasts` — what igni does, turned into signals: every
  message igni sends a window, every write a window makes, every
  extension state change (with :mod:`.observations` and
  :mod:`.window_signals`), delivered by :mod:`.signals` to the
  subscribers entitled to them, with :mod:`.windows` and :mod:`.grants`;
* :mod:`.channel` — the loopback socket an extension's process reaches it on.
"""
