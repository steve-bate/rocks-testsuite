/*
;;; Mudsync --- Live hackable MUD
;;; Copyright © 2017 Christopher Allan Webber <cwebber@dustycloud.org>
;;;
;;; This file is part of Mudsync.
;;;
;;; Mudsync is free software; you can redistribute it and/or modify it
;;; under the terms of the GNU General Public License as published by
;;; the Free Software Foundation; either version 3 of the License, or
;;; (at your option) any later version.
;;;
;;; Mudsync is distributed in the hope that it will be useful, but
;;; WITHOUT ANY WARRANTY; without even the implied warranty of
;;; MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
;;; General Public License for more details.
;;;
;;; You should have received a copy of the GNU General Public License
;;; along with Mudsync.  If not, see <http://www.gnu.org/licenses/>.
*/

function scrollDown() {
    var stream_metabox = document.getElementById("stream-metabox");
    stream_metabox.scrollTop = stream_metabox.scrollHeight;
}

function withMaybeScroll(thunk) {
    var stream_metabox = document.getElementById("stream-metabox");
    var should_scroll = false;
    // if within a reasonable threshold, we scroll
    if((stream_metabox.scrollHeight - stream_metabox.offsetHeight)
       - stream_metabox.scrollTop <= 50) {
        should_scroll = true;
    }
    thunk();
    if (should_scroll) {
        stream_metabox.scrollTop = stream_metabox.scrollHeight;
    }
}


function displayMessage(data) {
    var new_entry = document.createElement("div");
    withMaybeScroll(
        function () {
            new_entry.setAttribute("class", "stream-entry");
            new_entry.innerHTML = data;
            document.getElementById("stream").appendChild(new_entry);
        });
}

function setConnectedText(string, to_class) {
    var stream_metabox = document.getElementById("connection-status");
    stream_metabox.textContent = "[" + string + "]";
    stream_metabox.setAttribute("class", to_class);
}

function handleNoticeMessage(message_json, ws) {
    displayMessage(message_json["content"], false);
}

function handleInputPromptMessage(message_json, ws) {
    var centered_wrapper = document.createElement("div");
    var new_prompt = document.createElement("div");
    var button_metabox = document.createElement("div");
    var submit_button = document.createElement("button");
    var back_button = document.createElement("button");

    // Set up prompt div and surrounding wrapper
    centered_wrapper.setAttribute("class", "simple-centered-wrap");
    new_prompt.setAttribute("class", "prompt-user");
    new_prompt.setAttribute("id", "prompt-active");
    centered_wrapper.appendChild(new_prompt);

    // Fill prompt body with content
    new_prompt.innerHTML = message_json["content"];

    // Add prompt button box
    button_metabox.setAttribute("class", "prompt-button-metabox");
    submit_button.textContent = "Submit";
    back_button.textContent = "Back";
    button_metabox.appendChild(submit_button);
    if (message_json["can-go-back"]) {
        button_metabox.appendChild(back_button);
    };
    new_prompt.appendChild(button_metabox);

    // Set up callbacks on the buttons
    back_button.addEventListener(
        "click", function () {
            submitGoBack(ws);
        });
    submit_button.addEventListener(
        "click", function () {
            submitCurrentPrompt(ws);
        });

    // Finally, append the whole prompt and company to the stream
    withMaybeScroll(
        function () {
            document.getElementById("stream").appendChild(
                centered_wrapper);
        }
    );
}

var message_type_map = {
    "notice": handleNoticeMessage,
    "input-prompt": handleInputPromptMessage}

function delegateMessage(message_json, ws) {
    message_type_map[message_json["type"]](message_json, ws);
}

function installWebsocket() {
    // TODO: Don't hardcode the websocket path; pull it from the DOM
    var protocol = "ws://";
    if (window.location.protocol == "https:") {
        protocol = "wss://";
    }
    var address = protocol.concat(window.location.hostname, ":", window.location.port);
    var ws = new WebSocket(address);
    ws.onmessage = function(evt) {
        console.log(evt.data);
        delegateMessage(JSON.parse(evt.data), ws);
    };
    ws.onopen = function() {
        setConnectedText("connected", "connected");
        console.log("connected");
    };
    ws.onclose = function () {
        setConnectedText("disconnected", "disconnected");
        // kludge, we shouldn't be using self_sent like this because it
        // wipes the input
        const metabox = document.getElementById("stream-metabox")
        metabox.style.pointerEvents = "none";
        //metabox.style.opacity = 0.15;
        displayMessage(
            "* You have been disconnected.  Refresh to (hopefully) reconnect.",
            true);
        console.log("closed websocket");
    };
    // installUIHooks(ws);
}

function sendMessageToServer(ws, data) {
    ws.send(data);
}

function getActivePrompt() {
    return document.getElementById("prompt-active");
}

function processCheckbox(input) {
    return input.checked;
}

function processText(input) {
    return input.value;
}

var input_type_processors = {
    "checkbox": processCheckbox,
    "text": processText}

function getDataFromActivePrompt() {
    var prompt = getActivePrompt();
    var inputs = prompt.getElementsByTagName("input");
    var textareas = prompt.getElementsByTagName("textarea");
    var data = {};
    for (var i = 0; i < inputs.length; i++) {
        var input = inputs[i];
        // Dispatch to the processor for this type
        data[input["name"]] = input_type_processors[input["type"]](input);
    }
    for (var i = 0; i < textareas.length; i++) {
        var textarea = textareas[i];
        // Dispatch to the processor for this type
        data[textarea["name"]] = textarea.value;
    }
    return data;
}

function submitGoBack(ws) {
    disableActivePrompt();
    sendMessageToServer(ws, JSON.stringify({"action": "rewind"}));
}

function submitCurrentPrompt(ws) {
    var data = getDataFromActivePrompt();
    disableActivePrompt();
    console.log(data);
    sendMessageToServer(ws, JSON.stringify({"action": "send-input",
                                            "data": data}));
}

function disableActivePrompt() {
    var prompt = getActivePrompt();
    var inputs = prompt.getElementsByTagName("input");
    var textareas = prompt.getElementsByTagName("textarea");

    // Kludgily disable these
    for (var i = 0; i < inputs.length; i++) {
        inputs[i].setAttribute("disabled", "true");
    };
    for (var i = 0; i < textareas.length; i++) {
        textareas[i].setAttribute("disabled", "true");
    };

    // Set buttons to text that says *submitted*
    prompt.getElementsByClassName(
        "prompt-button-metabox")[0].innerHTML = "";

    // Set to disabled css class
    prompt.setAttribute("class", "prompt-user prompt-disabled");
    // Unset the prompt-active id
    prompt.removeAttribute("id");
}

window.onload = function () {
    installWebsocket();
    window.onresize = scrollDown;
}
