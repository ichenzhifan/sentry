import inspect

from sentry import projectoptions
from sentry.grouping.enhancer import Enhancements


STRATEGIES = {}


RISK_LEVEL_LOW = 0
RISK_LEVEL_MEDIUM = 1
RISK_LEVEL_HIGH = 2


def strategy(id=None, ids=None, interfaces=None, name=None, score=None):
    """Registers a strategy"""
    if interfaces is None:
        raise TypeError("interfaces is required")

    if name is None:
        if len(interfaces) != 1:
            raise RuntimeError("%r requires a name" % id)
        name = interfaces[0]

    if id is not None:
        if ids is not None:
            raise TypeError("id and ids given")
        ids = [id]

    def decorator(f):
        for id in ids:
            STRATEGIES[id] = rv = Strategy(
                id=id, name=name, interfaces=interfaces, score=score, func=f
            )
        return rv

    return decorator


class GroupingContext:
    def __init__(self, strategy_config):
        self._stack = [strategy_config.initial_context]
        self.config = strategy_config
        self.push()
        self["variant"] = None

    def __setitem__(self, key, value):
        self._stack[-1][key] = value

    def __getitem__(self, key):
        for d in reversed(self._stack):
            if key in d:
                return d[key]
        raise KeyError(key)

    def __enter__(self):
        self.push()
        return self

    def __exit__(self, exc_type, exc_value, tb):
        self.pop()

    def push(self):
        self._stack.append({})

    def pop(self):
        self._stack.pop()

    def get_grouping_component(self, interface, *args, **kwargs):
        """Invokes a delegate grouping strategy.  If no such delegate is
        configured a fallback grouping component is returned.
        """
        path = interface.path
        strategy = self.config.delegates.get(path)
        if strategy is None:
            raise RuntimeError(f"failed to dispatch interface {path} to strategy")

        kwargs["context"] = self
        rv = strategy(interface, *args, **kwargs)
        assert isinstance(rv, dict)

        if self["variant"] is not None:
            assert len(rv) == 1
            return rv[self["variant"]]

        return rv


def lookup_strategy(strategy_id):
    """Looks up a strategy by id."""
    try:
        return STRATEGIES[strategy_id]
    except KeyError:
        raise LookupError("Unknown strategy %r" % strategy_id)


def flatten_variants_from_component(component):
    """Given a component extracts variants from it if that component is
    a variant provider.  Otherwise this just returns the root component.
    """
    if not component.variant_provider:
        return {component.id: component}
    return {c.id: c for c in component.values}


class Strategy:
    """Baseclass for all strategies."""

    def __init__(self, id, name, interfaces, score, func):
        self.id = id
        self.strategy_class = id.split(":", 1)[0]
        self.name = name
        self.interfaces = interfaces
        self.score = score
        self.func = func
        self.variant_processor_func = None

    def __repr__(self):
        return f"<{self.__class__.__name__} id={self.id!r}>"

    def _invoke(self, func, *args, **kwargs):
        # We forcefully override strategy here.  This lets a strategy
        # function always access its metadata and directly forward it to
        # subcomponents without having to filter out strategy.
        kwargs["strategy"] = self
        return func(*args, **kwargs)

    def __call__(self, *args, **kwargs):
        return self._invoke(self.func, *args, **kwargs)

    def variant_processor(self, func):
        """Registers a variant reducer function that can be used to postprocess
        all variants created from this strategy.
        """
        self.variant_processor_func = func
        return func

    def get_grouping_component(self, event, context, variant=None):
        """Given a specific variant this calculates the grouping component."""
        args = []
        for iface_path in self.interfaces:
            iface = event.interfaces.get(iface_path)
            if iface is None:
                return None
            args.append(iface)
        with context:
            # If a variant is passed put it into the context
            if variant is not None:
                context["variant"] = variant
            return self(event=event, context=context, *args)

    def get_grouping_component_variants(self, event, context):
        """This returns a dictionary of all components by variant that this
        strategy can produce.
        """

        # strategy can decide on its own which variants to produce and which contribute
        variants = self.get_grouping_component(event, variant=None, context=context)
        if variants is None:
            return {}

        assert isinstance(variants, dict)

        if self.variant_processor_func is not None:
            variants = self._invoke(
                self.variant_processor_func, variants, event=event, context=context
            )
        return variants


class StrategyConfiguration:
    id = None
    base = None
    config_class = None
    strategies = {}
    delegates = {}
    changelog = None
    hidden = False
    risk = RISK_LEVEL_LOW

    def __init__(self, enhancements=None, **extra):
        if enhancements is None:
            enhancements = Enhancements([])
        else:
            enhancements = Enhancements.loads(enhancements)
        self.enhancements = enhancements

    def __repr__(self):
        return f"<{self.__class__.__name__} {self.id!r}>"

    def iter_strategies(self):
        """Iterates over all strategies by highest score to lowest."""
        return iter(sorted(self.strategies.values(), key=lambda x: -x.score))

    @classmethod
    def as_dict(self):
        return {
            "id": self.id,
            "base": self.base.id if self.base else None,
            "strategies": sorted(self.strategies),
            "changelog": self.changelog,
            "delegates": sorted(x.id for x in self.delegates.values()),
            "hidden": self.hidden,
            "risk": self.risk,
            "latest": projectoptions.lookup_well_known_key("sentry:grouping_config").get_default(
                epoch=projectoptions.LATEST_EPOCH
            )
            == self.id,
        }


def create_strategy_configuration(
    id,
    strategies=None,
    delegates=None,
    changelog=None,
    hidden=False,
    base=None,
    risk=None,
    initial_context=None,
):
    """Declares a new strategy configuration.

    Values can be inherited from a base configuration.  For strategies if there is
    a strategy of the same class it's replaced.  For delegates if there is a
    delegation for the same interface it's replaced.

    It's impossible to remove a strategy of a class when a base is declared (same
    for delegates).
    """

    class NewStrategyConfiguration(StrategyConfiguration):
        pass

    NewStrategyConfiguration.id = id
    NewStrategyConfiguration.base = base
    NewStrategyConfiguration.strategies = dict(base.strategies) if base else {}
    NewStrategyConfiguration.delegates = dict(base.delegates) if base else {}
    NewStrategyConfiguration.initial_context = dict(base.initial_context) if base else {}
    if risk is None:
        risk = RISK_LEVEL_LOW
    NewStrategyConfiguration.risk = risk
    NewStrategyConfiguration.hidden = hidden

    by_class = {}
    for strategy in NewStrategyConfiguration.strategies.values():
        by_class.setdefault(strategy.strategy_class, []).append(strategy.id)

    for strategy_id in strategies or {}:
        strategy = lookup_strategy(strategy_id)
        if strategy.score is None:
            raise RuntimeError(f"Unscored strategy {strategy_id} added to {id}")
        for old_id in by_class.get(strategy.strategy_class) or ():
            NewStrategyConfiguration.strategies.pop(old_id, None)
        NewStrategyConfiguration.strategies[strategy_id] = strategy

    new_delegates = set()
    for strategy_id in delegates or ():
        strategy = lookup_strategy(strategy_id)
        for interface in strategy.interfaces:
            if interface in new_delegates:
                raise RuntimeError(
                    "duplicate interface match for "
                    "delegate %r (conflict on %r)" % (id, interface)
                )
            NewStrategyConfiguration.delegates[interface] = strategy
            new_delegates.add(interface)

    if initial_context:
        NewStrategyConfiguration.initial_context.update(initial_context)

    NewStrategyConfiguration.changelog = inspect.cleandoc(changelog or "")
    NewStrategyConfiguration.__name__ = "StrategyConfiguration(%s)" % id
    return NewStrategyConfiguration


def produces_variants(variants):
    """
    A grouping strategy can either:

    - be told by the caller which variant to generate
    - determine its own variants

    In the latter case, use this decorator to produce variants and eliminate
    duplicate hashes.

    Syntax:
        # call decorated function twice with different variant values
        # (returning a new variant dictionary)
        @produces_variants(["system", "app"])

        # discard app variant if system variant produces the same hash, or if
        # the function returned None when invoked with `context['variant'] ==
        # 'system'`
        @produces_variants(["!system", "app"])
    """

    def decorator(f):
        def inner(*args, **kwargs):
            return call_with_variants(f, variants, *args, **kwargs)

        return inner

    return decorator


def call_with_variants(f, variants, *args, **kwargs):
    context = kwargs["context"]
    if context["variant"] is not None:
        # For the case where the variant is already determined, we act as a
        # delegate strategy.
        #
        # To ensure the function can deal with the particular value we assert
        # the variant name is one of our own though.
        assert context["variant"] in variants or "!" + context["variant"] in variants
        return f(*args, **kwargs)

    rv = {}
    has_mandatory_hashes = False
    mandatory_contributing_hashes = {}
    optional_contributing_variants = []
    prevent_contribution = None

    for variant in variants:
        is_mandatory = variant[:1] == "!"
        variant = variant.lstrip("!")

        with context:
            context["variant"] = variant
            variants = f(*args, **kwargs)
            assert len(variants) == 1
            component = variants[variant]

        if component is None:
            continue

        if is_mandatory:
            has_mandatory_hashes = True

        if component.contributes:
            if is_mandatory:
                mandatory_contributing_hashes[component.get_hash()] = variant
            else:
                optional_contributing_variants.append(variant)

        rv[variant] = component

    prevent_contribution = has_mandatory_hashes and not mandatory_contributing_hashes

    for variant in optional_contributing_variants:
        component = rv[variant]

        # In case this variant contributes we need to check two things
        # here: if we did not have a system match we need to prevent
        # it from contributing.  Additionally if it matches the system
        # component we also do not want the variant to contribute but
        # with a different message.
        if prevent_contribution:
            component.update(
                contributes=False,
                hint="ignored because %s variant is not used"
                % (
                    list(mandatory_contributing_hashes.values())[0]
                    if len(mandatory_contributing_hashes) == 1
                    else "other mandatory"
                ),
            )
        else:
            hash = component.get_hash()
            duplicate_of = mandatory_contributing_hashes.get(hash)
            if duplicate_of is not None:
                component.update(
                    contributes=False,
                    hint="ignored because hash matches %s variant" % duplicate_of,
                )

    return rv
