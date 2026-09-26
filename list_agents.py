from src.backend.v4.config.agent_registry import agent_registry

agents = agent_registry.get_all_agents()
names = [getattr(a, 'agent_name', getattr(a, 'name', type(a).__name__)) for a in agents]
print(names)
