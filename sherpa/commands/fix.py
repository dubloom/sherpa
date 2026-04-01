from sherpa.commands.base import Command

class FixCommand(Command):
    @staticmethod
    def execute_command(args: list[str], model: str):
        return super().execute_command(args, model)