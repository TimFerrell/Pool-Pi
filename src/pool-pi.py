from distutils.log import INFO
from commands import *
from threading import Thread
from model import *
from web import *
from parsing import *
from os import makedirs
from os.path import exists
from os import stat
import logging
from logging.handlers import TimedRotatingFileHandler


def readSerialBus(serialHandler):
    """
    Read data from the serial bus to build full frame in buffer.
    Serial frames begin with DLE STX and terminate with DLE ETX.
    With the exception of searching for the two start bytes,
    this function only reads one byte to prevent blocking other processes.
    When looking for start of frame, looking_for_start is True.
    When buffer is filled with a full frame and ready to be parseed,
    buffer_full is set to True to signal parseBuffer.
    """
    if serialHandler.in_waiting() == 0:  # Check if we have serial data to read
        return
    if (
        serialHandler.buffer_full == True
    ):  # Check if we already have a full frame in buffer
        return
    serChar = serialHandler.read()
    if serialHandler.looking_for_start:
        # We are looking for DLE STX to find beginning of frame
        if serChar == DLE:
            serChar = serialHandler.read()
            if serChar == STX:
                # We have found start (DLE STX)
                serialHandler.buffer.clear()
                serialHandler.buffer += DLE
                serialHandler.buffer += STX
                serialHandler.looking_for_start = False
                return
            else:
                # We have found DLE but not DLE STX
                return
        else:
            # Non-DLE character
            # We are only interested in DLE to find potential start
            return
    else:
        # We have already found the start of the frame
        # We are adding to buffer while looking for DLE ETX
        serialHandler.buffer += serChar
        # Check if we have found DLE ETX
        if (serChar == ETX) and (
            serialHandler.buffer[-2] == int.from_bytes(DLE, "big")
        ):
            # We have found a full frame
            serialHandler.buffer_full = True
            serialHandler.looking_for_start = True
            return


def parseBuffer(poolModel, serialHandler, commandHandler):
    """
    Check if we have full frame in buffer.
    If we have a full frame in buffer, parse it.
    If frame is keep alive, check to see if we are ready to send a command and if so send it.
    """
    if serialHandler.buffer_full:
        frame = serialHandler.buffer
        # Remove any extra x00 after x10
        frame = frame.replace(b"\x10\x00", b"\x10")

        # Ensure no erroneous start/stop within frame
        if b"\x10\x02" in frame[2:-2]:
            logging.error(f"DLE STX in frame: {frame}")
            serialHandler.reset()
            return
        if b"\x10\x03" in frame[2:-2]:
            logging.error(f"DLE ETX in frame: {frame}")
            serialHandler.reset()
            return

        # Compare calculated checksum to frame checksum
        if confirmChecksum(frame) == False:
            # If checksum doesn't match, message is invalid.
            # Clear buffer and don't attempt parsing.
            serialHandler.reset()
            return

        # Extract type and data from frame
        frameType = frame[2:4]
        data = frame[4:-4]

        # Use frame type to determine parsing function
        if frameType == FRAME_TYPE_KEEPALIVE:
            # Check to see if we have a command to send
            if serialHandler.ready_to_send == True:
                if commandHandler.keep_alive_count == 1:
                    # If this is the second sequential keep alive frame, send command
                    serialHandler.send(commandHandler.full_command)
                    logging.info(
                        f"Sent: {commandHandler.parameter}, {commandHandler.full_command}"
                    )
                    if commandHandler.confirm == False:
                        commandHandler.sending_message = False
                    serialHandler.ready_to_send = False
                else:
                    commandHandler.keep_alive_count = 1
            else:
                commandHandler.keep_alive_count = 0
        else:
            # Message is not keep alive
            commandHandler.keep_alive_count = 0
            if frameType == FRAME_TYPE_DISPLAY:
                parseDisplay(data, poolModel)
            elif frameType == FRAME_TYPE_LEDS:
                parseLEDs(data, poolModel)
            elif frameType == FRAME_TYPE_DISPLAY_SERVICE:
                parseDisplay(data, poolModel)
            elif frameType == FRAME_TYPE_SERVICE_MODE:
                logging.info(f"Service Mode update: {frameType}, {data}")
            # TODO add parsing and logging for local display commands
            # not sent by Pool-Pi (\x00\x02)
            else:
                logging.info(f"Unkown update: {frameType}, {data}")
        # Clear buffer and reset flags
        serialHandler.reset()


def checkCommand(poolModel, serialHandler, commandHandler):
    """
    If we are trying to send a message, wait for a new pool model to get pool states
    If necessary, queue message to be sent after second keep alive
    """
    if commandHandler.sending_message == False:
        # We aren't trying to send a command, nothing to do
        return

    if serialHandler.ready_to_send == True:
        # We are already ready to send, awaiting keep alive
        return

    if poolModel.timestamp >= commandHandler.last_model_timestamp_seen:
        # We have a new poolModel
        if (
            poolModel.getParameterState(commandHandler.parameter)
            == commandHandler.target_state
        ):
            # Model matches, command was successful.
            # Reset sending state.
            logging.info(f"Command success.")
            commandHandler.sending_message = False
            poolModel.sending_message = False
            poolModel.flag_data_changed = True
        else:
            # New poolModel doesn't match, command not successful.
            if commandHandler.sendAttemptsRemain() == True:
                commandHandler.last_model_timestamp_seen = time.time()
                serialHandler.ready_to_send = True


def getCommand(poolModel, serialHandler, commandHandler):
    """
    If we're not currently sending a command, check if there are new commands.
    Get new command from command_queue, validate, and initiate send with commandHandler.
    """
    # TODO figure out threading issue or move command_queue to tmp directory
    if commandHandler.sending_message == True:
        # We are currently trying to send a command, don't need to check for others
        return
    if exists("command_queue.txt") == False:
        return
    if stat("command_queue.txt").st_size != 0:
        f = open("command_queue.txt", "r+")
        line = f.readline()
        try:
            if len(line.split(",")) == 2:
                # Extract csv command info
                commandID = line.split(",")[0]
                frontEndVersion = int(line.split(",")[1])

                if frontEndVersion != poolModel.version:
                    logging.error(
                        f"Invalid command: Back end version is {poolModel.version} but front end version is {frontEndVersion}."
                    )
                    f.truncate(0)
                    f.close()
                    return

                # Determine if command requires confirmation
                if (commandID in button_toggle) or (commandID == "pool-spa-spillover"):
                    commandConfirm = True
                elif commandID in buttons_menu:
                    commandConfirm = False
                else:
                    # commandID has no match in commands.py
                    logging.error(
                        f"Invalid command: Error parsing command: {commandID}"
                    )
                    # Clear file contents
                    f.truncate(0)
                    f.close()
                    return

                if commandConfirm == True:
                    # Command is not a menu button.
                    # Confirmation if command was successful is needed

                    # Pool spa spillover is single button- need to get individual commandID
                    if commandID == "pool-spa-spillover":
                        if poolModel.getParameterState("pool") == "ON":
                            commandID = "pool"
                        elif poolModel.getParameterState("spa") == "ON":
                            commandID = "spa"
                        else:
                            commandID = "spillover"

                    # Check we aren't in INIT state
                    if poolModel.getParameterState(commandID) == "INIT":
                        logging.error(
                            f"Invalid command: Target parameter {commandID} is in INIT state."
                        )
                        f.close()
                        return
                    # Determine next desired state
                    currentState = poolModel.getParameterState(commandID)
                    # Service tristate ON->BLINK->OFF
                    if commandID == "service":
                        if currentState == "ON":
                            desiredState = "BLINK"
                        elif currentState == "BLINK":
                            desiredState = "OFF"
                        else:
                            desiredState = "ON"
                    # All other buttons
                    else:
                        if currentState == "ON":
                            desiredState = "OFF"
                        else:
                            desiredState = "ON"

                    logging.info(
                        f"Valid command: {commandID} {desiredState}, version {frontEndVersion}"
                    )
                    # Push to command handler
                    commandHandler.initiateSend(commandID, desiredState, commandConfirm)
                    poolModel.sending_message = True

                else:
                    # Command is a menu button
                    # No confirmation needed. Only send once.
                    # Immediately load for sending.
                    commandHandler.initiateSend(commandID, "NA", commandConfirm)
                    serialHandler.ready_to_send = True
            else:
                logging.error(f"Invalid command: Command structure is invalid: {line}")
        except Exception as e:
            logging.error(f"Invalid command: Error parsing command: {line}, {e}")
        # Clear file contents
        f.truncate(0)
        f.close()
    return


def sendModel(poolModel):
    """
    Check if we have new date for the front end. If so, send data as JSON.
    """
    if poolModel.flag_data_changed == True:
        socketio.emit("model", poolModel.toJSON())
        logging.debug("Sent model")
        poolModel.flag_data_changed = False
    return


def main():
    poolModel = PoolModel()
    serialHandler = SerialHandler()
    commandHandler = CommandHandler()
    if exists("command_queue.txt") == True:
        if stat("command_queue.txt").st_size != 0:
            f = open("command_queue.txt", "r+")
            f.truncate(0)
            f.close()
    while True:
        # Read Serial Bus
        # If new serial data is available, read from the buffer
        readSerialBus(serialHandler)

        # Parse Buffer
        # If a full serial frame has been found, decode it and update model.
        # If we have a command ready to be sent, send.
        parseBuffer(poolModel, serialHandler, commandHandler)

        # If we are sending a command, check if command needs to be sent.
        # Check model for updates to see if command was accepted.
        checkCommand(poolModel, serialHandler, commandHandler)

        # Send updates to front end.
        sendModel(poolModel)

        # If we're not sending, check for new commands from front end.
        getCommand(poolModel, serialHandler, commandHandler)


if __name__ == "__main__":
    # Create log file directory if not already existing
    if not exists("logs"):
        makedirs("logs")
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    handler = TimedRotatingFileHandler(
        "logs/pool-pi.log", when="midnight", backupCount=60
    )
    handler.suffix = "%Y-%m-%d_%H-%M-%S"
    handler.setFormatter(formatter)
    logging.getLogger().handlers.clear()
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    logging.info("Started pool-pi.py")
    Thread(
        target=lambda: socketio.run(
            app, debug=False, host="0.0.0.0", allow_unsafe_werkzeug=True
        )
    ).start()
    Thread(target=main).start()
