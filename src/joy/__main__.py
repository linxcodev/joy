import sys
import argparse
from rich.console import Console
from joy.cli import main, setup_session_commands

def create_argument_parser():
    """Create argument parser with all Joy commands."""
    parser = argparse.ArgumentParser(
        prog='joy',
        description='Joy - Coding agent for LLM-powered development',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  joy                          # Start interactive session
  joy resume [session_id]      # Resume previous session
  joy list-sessions            # Show saved sessions
  joy session-status [id]      # Show session details
  joy delete-session [id]      # Delete a session
        """,
    )
    
    # Add session subcommands
    setup_session_commands(parser)
    
    return parser

def main_entry():
    """Main entry point with argument parsing."""
    parser = create_argument_parser()
    
    # Parse only if there are arguments (not interactive mode)
    if len(sys.argv) > 1:
        args = parser.parse_args()
    else:
        args = argparse.Namespace(session_command=None)
    
    console = Console()
    main(console=console, args=args)

if __name__ == "__main__":
    main_entry()
