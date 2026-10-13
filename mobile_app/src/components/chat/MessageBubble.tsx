import { memo } from 'react';
import { Text, View } from 'react-native';

import type { ChatMessage } from './types';

type Props = { message: ChatMessage };

/**
 * One chat message. User: right, blue-600, white text. Assistant: left, slate-800, gray-100 text.
 *
 * The body is a plain <Text> for now. When markdown is needed (the assistant streams it),
 * swap the <Text> below for a markdown renderer: this is the only place that has to change.
 */
function MessageBubbleBase({ message }: Props) {
  const mine = message.role === 'user';
  return (
    <View className={`mb-2 w-full flex-row ${mine ? 'justify-end' : 'justify-start'}`}>
      <View
        className={`max-w-[82%] rounded-2xl px-4 py-3 ${
          mine ? 'rounded-br-md bg-blue-600' : 'rounded-bl-md border border-slate-700/60 bg-slate-800'
        }`}>
        <Text selectable className={`text-base leading-6 ${mine ? 'text-white' : 'text-gray-100'}`}>
          {message.text}
        </Text>
      </View>
    </View>
  );
}

export const MessageBubble = memo(MessageBubbleBase);
