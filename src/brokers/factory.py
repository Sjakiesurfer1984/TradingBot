from __future__ import annotations

from dataclasses import dataclass

from src.brokers.interfaces import BrokerABC
from src.brokers.registry import BrokerBuilderRegistryABC
from src.config.env_config import EnvConfig
from src.utilities.logger import setup_logger

logger = setup_logger("BrokerFactory")


@dataclass(frozen=True)
class BrokerFactoryFacade:
    registry: BrokerBuilderRegistryABC

    def build_broker(self, *, env_cfg: EnvConfig) -> BrokerABC:
        name = str(env_cfg.broker_name).strip()
        logger.info("Building broker | name=%s", name)
        broker = self.registry.get_builder(name).build(env_cfg)
        logger.info("Broker built | type=%s", type(broker).__name__)
        return broker

'''
A factory is a design pattern that provides a way to create objects without specifying 
the exact class of the object that will be created. In this implementation, the BrokerFactoryFacade 
serves as a factory for creating broker instances based on the provided environment configuration. 
It uses a registry of broker builders to construct the appropriate broker instance based on the broker name specified 
in the environment configuration. This allows for flexibility and extensibility, as new brokers can be added by simply 
registering new builders in the registry without modifying the factory facade itself.

It also abstracts away the construction logic from the main application code, making it cleaner and more maintainable.

The factory does not contain any broker-specific logic; it simply delegates the construction to the appropriate builder based on the configuration.
It knows which builder to "call" based on the broker name, but it does not know how the builder constructs the broker.
The builder in this case is the AlpacaBrokerBuilder, which knows how to construct an Alpaca broker instance from the environment configuration.
It knows this because it implements the build method defined in the BrokerBuilderABC interface, which is called by the factory facade when building the broker.

So the sequence is:
- The main application code calls BrokerFactoryFacade.build_broker(env_cfg).
- The factory facade retrieves the broker name from env_cfg and looks up the corresponding builder in the registry: "Hi, it's FactoryFacade. 
I need to build a broker. Let me check the registry for the builder registered under the name specified in env_cfg."
- The registry returns the appropriate builder (e.g., AlpacaBrokerBuilder).
- The factory facade calls the build method of the retrieved builder, passing in the env_cfg.
- The builder constructs and returns the broker instance, which is then returned by the factory facade to the main application code.
This design allows for a clean separation of concerns and adheres to the Open/Closed Principle,
as new brokers can be added without modifying existing code, and the factory facade remains unchanged regardless of how many brokers are added in the future.
so: call factory -> factory looks up builder in registry -> factory calls builder.build(env_cfg) -> builder constructs and returns broker instance -> factory returns broker instance to caller.
'''