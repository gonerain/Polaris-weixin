"""Install the real independent reply provider into an isolated test runtime."""
from components import manifest
from components.assistant import Assistant


def install(runtime):
    provider = Assistant()
    runtime.install(manifest('assistant'), lambda: provider)
    runtime.start_component('assistant')
    for op in ('reply', 'status'):
        runtime.grant('user', 'assistant', op)
    return provider


def delivered_history(case):
    channel = case.component.agent
    if not channel.parent:
        return []
    key = (case.context.origin_installation, case.identity, channel.conversation)
    return case.assistant._conversations[key][channel.parent]


def seed_history(case, messages):
    channel = case.component.agent
    key = (case.context.origin_installation, case.identity, channel.conversation)
    channel.parent = 'seed'
    case.assistant._conversations[key] = {'seed':messages}
