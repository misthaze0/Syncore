/** Cache one voice turn so reloading the main window can recover its live UI. */
function createPetVoiceRelay() {
  let turn = null;
  let sequence = 0;
  return {
    get: () => turn && structuredClone(turn),
    accept(input) {
      if (!input || typeof input !== 'object' || typeof input.id !== 'string' || input.id.length > 100) return null;
      if (input.type === 'start') {
        if (typeof input.message !== 'string' || !input.message.trim() || input.message.length > 10000) return null;
        turn = { id: input.id, message: input.message, events: [], complete: false, sequence: ++sequence };
        return { type: 'start', id: turn.id, message: turn.message, sequence };
      }
      if (!turn || turn.id !== input.id || turn.complete) return null;
      if (input.type === 'end') {
        turn.complete = true;
        turn.sequence = ++sequence;
        return { type: 'end', id: turn.id, sequence };
      }
      if (input.type !== 'event' || !input.event || typeof input.event.type !== 'string') return null;
      const event = structuredClone(input.event);
      if (JSON.stringify(event).length > 256000) return null;
      // Text snapshots supersede earlier text, preventing quadratic cache growth.
      if (event.type === 'assistant_messages') {
        turn.events = turn.events.filter((item) => !['token', 'assistant_messages'].includes(item.type));
      }
      const previous = turn.events.at(-1);
      if (['token', 'reasoning'].includes(event.type) && previous?.type === event.type &&
          previous.message_index === event.message_index && typeof event.content === 'string') {
        previous.content += event.content;
      } else {
        turn.events.push(event);
      }
      turn.sequence = ++sequence;
      return { type: 'event', id: turn.id, event, sequence };
    },
    end() {
      return turn && !turn.complete ? this.accept({ type: 'end', id: turn.id }) : null;
    },
  };
}

module.exports = { createPetVoiceRelay };
